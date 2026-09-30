"""AI 把候選新聞讀一遍：是不是新聞、跟哪家客戶真的相關、記者是誰。

走 Anthropic Messages API 協定（官方 SDK），base_url 可指向 Claude 官方，
或 Kimi／z.ai 的 Anthropic 相容端點；設定在 defaults.yaml 的 `ai:`。

AI 判斷失敗（逾時、內容審查拒答、JSON 壞掉）的那則回傳 None，
呼叫端照原本的規則處理，絕不因 AI 出錯而丟新聞。
"""

import json, os, re, sys
from pathlib import Path

from common import ROOT, load_state, pmap, save_state

PROMPT_VER = "v2"   # 提示或輸出格式改版時要跟著改，快取才會重判

SYSTEM = """你是台灣一家媒體公關公司的新聞監測助理。公司每天替上市櫃客戶整理「跟客戶有關的新聞」，由同仁最後查核後交給客戶。

你會收到幾則候選新聞，每則附上媒體、標題、內文前段，以及「候選客戶」：這則是因為哪些客戶的哪些搜尋詞被找到的。請逐則判斷：

1. is_news：這是不是一篇新聞或評論文章。站內搜尋結果頁、AI 問答頁、基金或股票資料頁、行情報價頁、商品頁、目錄頁都不是新聞。
2. reporter：內文或「署名線索」裡明確署名的記者或作者姓名，例如「記者王小明／台北報導」填「王小明」，多位用「、」分隔。編譯、綜合報導、中央社電、只有媒體名、沒有署名，一律填空字串。不要猜。
3. clients：對每個候選客戶判斷 relevant（true/false）與 reason（15 字內繁體中文）。
   - 相關：報導這家公司本身；或屬於這家客戶搜尋主題的產業新聞（例如搜尋主題有「被動元件」，MLCC 產業新聞就相關）。
   - 不相關：只是字面撞名（例如電視台週年的「台慶」不是名稱相近的公司、形容詞「精湛」不是公司簡稱）、娛樂新聞、與搜尋主題無關的內容。

只輸出一個 JSON 陣列，不要其他文字：
[{"id": "a1", "is_news": true, "reporter": "王小明", "clients": {"客戶代號": {"relevant": true, "reason": "報導公司營收"}}}]"""


def load_env():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$", line)
        if m and m.group(2) and not os.environ.get(m.group(1)):
            os.environ[m.group(1)] = m.group(2).strip().strip('"').strip("'")


def page_for_ai(page):
    """從網頁取出要給 AI 的兩段：正文前段，以及署名線索。
    署名常在正文以外（標題區、文末），所以另外撈「記者／作者／撰文／報導」附近的字。"""
    from common import article_text, plain_text
    if not page:
        return "", ""
    full = plain_text(page)
    clues = re.findall(r".{0,12}(?:記者|作者|撰文|文／|文/|編譯|報導)[^。]{0,16}", full)
    m = re.search(r'"author"\s*:\s*[\[{].{0,200}?"name"\s*:\s*"([^"]{1,40})"', page, re.S)
    if m:
        clues.insert(0, f"author 欄位：{m.group(1)}")
    return article_text(page), "｜".join(dict.fromkeys(c.strip() for c in clues))[:400]


def _client(cfg):
    """provider: anthropic（Claude 或 Kimi／z.ai 的 Anthropic 相容端點）或 openai。"""
    load_env()
    openai_mode = cfg.get("provider") == "openai"
    key_env = cfg.get("key_env") or ("OPENAI_API_KEY" if openai_mode else "ANTHROPIC_API_KEY")
    key = os.environ.get(key_env)
    if not key:
        raise RuntimeError(f"找不到 API 金鑰：.env 的 {key_env}")
    if openai_mode:
        import openai
        return openai.OpenAI(api_key=key, base_url=cfg.get("base_url") or None,
                             timeout=cfg.get("timeout", 300), max_retries=1)
    import anthropic
    return anthropic.Anthropic(api_key=key, base_url=cfg.get("base_url") or None,
                               timeout=cfg.get("timeout", 300), max_retries=1)


def _parse(text):
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        raise ValueError("回應裡沒有 JSON 陣列")
    return json.loads(m.group(0))


def _ask(client, cfg, batch):
    parts = []
    for it in batch:
        cands = "\n".join(f"  - {cid}（{c['name']}）搜尋主題：{'、'.join(c['topics']) or '公司名'}"
                          for cid, c in it["clients"].items())
        parts.append(f"### id: {it['id']}\n媒體：{it['source']}\n標題：{it['title']}\n"
                     f"候選客戶：\n{cands}\n署名線索：{it.get('clues') or '（無）'}\n"
                     f"內文前段：\n{it['text'] or '（抓不到內文）'}")
    prompt = "\n\n".join(parts)
    if cfg.get("provider") == "openai":
        r = client.responses.create(model=cfg["model"], instructions=SYSTEM, input=prompt,
                                    max_output_tokens=cfg.get("max_tokens", 16000))
        text, u = r.output_text, r.usage
        usage = (u.input_tokens, u.output_tokens) if u else (0, 0)
    else:
        r = client.messages.create(model=cfg["model"], max_tokens=cfg.get("max_tokens", 16000),
                                   system=SYSTEM, messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in r.content if b.type == "text")
        usage = (r.usage.input_tokens or 0, r.usage.output_tokens or 0)
    out = {str(x.get("id")): x for x in _parse(text)}
    missing = [it["id"] for it in batch if it["id"] not in out]
    if missing:
        raise ValueError(f"回應少了 {missing}")
    return out, usage


def _normalize(ans, clients):
    res = {"is_news": bool(ans.get("is_news", True)),
           "reporter": str(ans.get("reporter") or "").strip(), "clients": {}}
    for cid in clients:
        v = (ans.get("clients") or {}).get(cid) or {}
        res["clients"][cid] = {"relevant": bool(v.get("relevant", True)),
                               "reason": str(v.get("reason") or "").strip()[:40]}
    return res


def _ck(cfg, it):
    return f"{cfg['model']}|{PROMPT_VER}|{it['key']}|{','.join(sorted(it['clients']))}"


def uncached(items, cfg):
    """還沒判讀過的那幾則（要先抓網頁才能送給 AI）。"""
    cache = load_state("aijudge.json", {})
    return [it for it in items if _ck(cfg, it) not in cache]


def judge(items, cfg, log=print):
    """items: [{key, source, title, text, clients: {cid: {name, topics}}}]
    回傳 {key: 結果 or None}。結果：{is_news, reporter, clients: {cid: {relevant, reason}}}"""
    cache = load_state("aijudge.json", {})
    ck = lambda it: _ck(cfg, it)
    results, todo = {}, []
    for it in items:
        hit = cache.get(ck(it))
        if hit:
            results[it["key"]] = hit["r"]
        else:
            todo.append(it)
    if not todo:
        return results

    client = _client(cfg)
    size = cfg.get("batch", 6)
    for n, it in enumerate(todo):
        it["id"] = f"a{n + 1}"
        it["text"] = (it.get("text") or "")[:cfg.get("text_chars", 3000)]
    batches = [todo[i:i + size] for i in range(0, len(todo), size)]
    usage = {"in": 0, "out": 0}
    failed = []

    def run(batch):
        try:
            return batch, _ask(client, cfg, batch), None
        except Exception as e:
            return batch, None, e

    def settle(batch, got):
        answers, u = got
        usage["in"] += u[0] or 0
        usage["out"] += u[1] or 0
        for it in batch:
            r = _normalize(answers[it["id"]], it["clients"])
            results[it["key"]] = r
            cache[ck(it)] = {"r": r, "d": cfg.get("today", "")}

    log(f"  AI 判讀 {len(todo)} 則（{cfg['model']}，每次 {size} 則，其餘走快取）…")
    retry = []
    for batch, got, err in pmap(run, batches, cfg.get("workers", 4)):
        if got:
            settle(batch, got)
        else:
            retry += batch   # 整批失敗（常見是其中一則被內容審查擋下）→ 逐則重試
    for batch, got, err in pmap(run, [[it] for it in retry], cfg.get("workers", 4)):
        if got:
            settle(batch, got)
        else:
            results[batch[0]["key"]] = None
            failed.append((batch[0]["title"][:30], str(err)[:80]))

    keep_after = cfg.get("keep_after", "")
    save_state("aijudge.json", {k: v for k, v in cache.items() if v.get("d", "") >= keep_after})
    log(f"  AI 判讀完成：用量 輸入 {usage['in']:,}／輸出 {usage['out']:,} tokens"
        + (f"；{len(failed)} 則 AI 未判斷，照規則處理" if failed else ""))
    for title, err in failed[:5]:
        print(f"    · 未判斷：{title}…（{err}）", file=sys.stderr)
    return results
