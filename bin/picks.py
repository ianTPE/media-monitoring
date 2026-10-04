# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "langfuse"]
# ///
"""人工篩選結果當標準答案：匯入 Langfuse 資料集，並比對系統表現。

  ./monitor picks import --date 2026-09-30 [檔案]   檔案省略＝從標準輸入貼上
  ./monitor picks eval                              在 Langfuse 跑一次比對（不呼叫 Luna）

貼上的格式就是同仁整理好的監測結果，例如：
  晟德【 9/30 新聞監測】
  1.經濟日報 任君翔
  長佳智能宣布實施庫藏股 預計買回1,714張
  https://money.udn.com/money/story/5612/9784024
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tracing
from common import ROOT, clean_url, find_existing, load_clients, url_key
from email_digest import parse_candidates
from judgment_rules import keep_company_title

DATASET = "人工篩選"
HEADER = re.compile(r"([^\s【]+)【\s*\d+\s*/\s*\d+\s*新聞監測】")
URL = re.compile(r"https?://\S+")


def key_of(url):
    return url_key(clean_url(url))


def norm_title(title):
    return re.sub(r"[\s　,，、。：:！!？?「」『』（）()\[\]【】\-－—…·.｜|]+", "", title)[:14]


def parse_picks(text, clients):
    """回傳 ({客戶id: [{source, title, url}]}, 有出現在貼上內容的客戶id集合)。

    有出現的客戶才算「審過」：沒出現的可能是別人負責，不能當成全部沒選。
    貼上內容被擠成一行時，先在客戶標題、編號、網址前後斷行。"""
    text = re.sub(r"\s*(https?://\S+)\s*", r"\n\1\n", text)
    text = re.sub(r"\s*([^\s【]+【\s*\d+\s*/\s*\d+\s*新聞監測】)\s*", r"\n\1\n", text)
    text = re.sub(r"\s+(\d{1,2}\.)(?=\S)", r"\n\1", text)
    text = re.sub(r"\s*-{3,}\s*", "\n", text)
    names = {c["name"]: c["id"] for c in clients}
    picks, reviewed, current, recent = {}, set(), None, []
    for raw in text.splitlines():
        line = raw.strip()
        head = HEADER.search(line)
        if head:
            current = names.get(head.group(1))
            if current:
                reviewed.add(current)
            if current is None:
                print(f"  ！認不得客戶「{head.group(1)}」，這一段略過", file=sys.stderr)
            recent = []
            continue
        if not line or set(line) <= set("-—"):
            continue
        url = URL.search(line)
        if url and current:
            title = recent[-1] if recent else ""
            source = re.sub(r"^\d+\.\s*", "", recent[-2]) if len(recent) > 1 else ""
            picks.setdefault(current, [])
            if all(key_of(p["url"]) != key_of(url.group(0)) for p in picks[current]):
                picks[current].append({"source": source.strip(), "title": title,
                                       "url": url.group(0)})
            recent = []
        elif "無候選新聞" not in line:
            recent.append(line)
    return picks, reviewed


def candidates_for(cfg, day):
    path = find_existing("candidates", cfg, day)
    return parse_candidates(path) if path.is_file() else None


def in_email(cfg, item):
    """跟寄信程式同一套規則：AI 判為雜訊且沒勾、也不是客戶本身的新聞，就不進信件。"""
    return (not item["ai_rejected"] or item["checked"]
            or keep_company_title(cfg, item["title"], item["url"]))


def find(items, url, title):
    k, t = key_of(url), norm_title(title)
    for it in items:
        if it["url"] and key_of(it["url"]) == k:
            return it
    for it in items:      # 同一篇報導換了網址（例如經濟日報版與聯合報版）
        if t and norm_title(it["title"]) == t:
            return it
    return None


def _api(method, path, **params):
    """直接呼叫 Langfuse 公開 API（列出／刪除資料集項目）。"""
    import base64, os, urllib.parse, urllib.request
    tracing.client()            # 確保已載入 .env 的 LANGFUSE_*
    host = os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL")
    token = base64.b64encode(f"{os.environ['LANGFUSE_PUBLIC_KEY']}:"
                             f"{os.environ['LANGFUSE_SECRET_KEY']}".encode()).decode()
    url = f"{host}/api/public/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    req = urllib.request.Request(url, method=method, headers={"Authorization": f"Basic {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return json.loads(body) if body else {}


def items_of_day(day):
    out, page = [], 1
    while True:
        res = _api("GET", "dataset-items", datasetName=DATASET, limit=100, page=page)
        out += [it for it in res.get("data", []) if (it.get("input") or {}).get("日期") == str(day)]
        if page >= res.get("meta", {}).get("totalPages", 1):
            return out
        page += 1


def cmd_import(args):
    day = date.fromisoformat(args.date)
    text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    clients = load_clients()
    picks, reviewed = parse_picks(text, clients)
    if not reviewed:
        raise SystemExit("沒有解析到任何客戶的監測結果，請確認貼上的格式")
    # 同一天可分次匯入（例如兩位同仁各審一半）：審過的客戶取聯集，這次有審的客戶以這次為準
    folder = ROOT / ".state" / "picks"
    folder.mkdir(parents=True, exist_ok=True)
    saved = folder / f"{day}.json"
    old = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else {}
    if "reviewed" not in old:                       # 舊格式：只有 picks，且當時沒記審過哪些客戶
        old = {"reviewed": list(old), "picks": old}
    picks = {**{k: v for k, v in old["picks"].items() if k not in reviewed}, **picks}
    reviewed = reviewed | set(old["reviewed"]) if args.add else reviewed
    if not args.add:
        picks = {k: v for k, v in picks.items() if k in reviewed}
    saved.write_text(json.dumps({"reviewed": sorted(reviewed), "picks": picks},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    with open(folder / f"{day}.txt", "a" if args.add else "w", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n\n")

    lf = tracing.client()
    if lf is None:
        raise SystemExit("Langfuse 未啟用（.env 缺 LANGFUSE_*）；人工篩選已存在 .state/picks/")
    try:
        lf.get_dataset(DATASET)
    except Exception:
        lf.create_dataset(name=DATASET, description="每天同仁人工篩選的結果，當作系統判讀的標準答案")

    # 這天先前匯入、但這次沒審到的客戶，舊資料刪掉（那些客戶可能是別人審的）
    stale = [it for it in items_of_day(day) if it["input"].get("客戶代號") not in reviewed]
    for it in stale:
        _api("DELETE", f"dataset-items/{it['id']}")
    if stale:
        print(f"  刪除 {day} 未審客戶的舊資料 {len(stale)} 筆")

    rows, missed = [], 0
    for cfg in [c for c in clients if c["id"] in reviewed]:     # 沒出現的客戶可能是別人審的
        cands = candidates_for(cfg, day)
        chosen = picks.get(cfg["id"], [])
        if cands is None:
            if chosen:
                print(f"  ！{cfg['name']} 找不到 {day} 的候選檔，只匯入人工選的", file=sys.stderr)
            cands = []
        # 一則人工選的只對應一筆候選：先比網址，再比標題（同篇換網址的情況）
        hit_of = {}
        for p in chosen:
            it = find([c for c in cands if id(c) not in hit_of.values()], p["url"], p["title"])
            if it is not None:
                hit_of[id(p)] = id(it)
        picked = set(hit_of.values())
        for it in cands:
            rows.append((cfg, it["title"], it["source"], it["url"], id(it) in picked, True))
        for p in chosen:
            if id(p) not in hit_of:
                missed += 1
                rows.append((cfg, p["title"], p["source"], p["url"], True, False))

    for cfg, title, source, url, selected, listed in rows:
        lf.create_dataset_item(
            dataset_name=DATASET,
            id=hashlib.sha1(f"{day}|{cfg['id']}|{key_of(url)}".encode()).hexdigest()[:24],
            input={"日期": str(day), "客戶": cfg["name"], "客戶代號": cfg["id"],
                   "媒體": source, "標題": title, "網址": url},
            expected_output={"人工選了": selected},
            metadata={"在系統候選中": listed})
    tracing.flush()
    n = sum(len(v) for v in picks.values())
    print(f"  已匯入 {day}：審過 {len(reviewed)} 家，人工選了 {n} 則，"
          f"資料集共寫入 {len(rows)} 筆；系統候選沒有的 {missed} 則")
    if missed:      # 有漏抓就順便分析原因、請 AI 提搜尋詞，給人勾選
        from propose import cmd_propose
        cmd_propose(argparse.Namespace(date=str(day)))


def task(*, item, **kwargs):
    inp = item.input
    cfg = next(c for c in load_clients() if c["id"] == inp["客戶代號"])
    cands = candidates_for(cfg, date.fromisoformat(inp["日期"]))
    it = find(cands or [], inp["網址"], inp["標題"])
    if it is None:
        return {"進信件": False, "原因": "不在系統候選中"}
    return {"進信件": in_email(cfg, it), "預設勾選": it["checked"],
            "AI判為雜訊": it["ai_rejected"]}


def evaluator(*, input, output, expected_output, **kwargs):
    from langfuse import Evaluation
    if expected_output["人工選了"]:
        ok = output["進信件"]
        return Evaluation(name="召回", value=1.0 if ok else 0.0,
                          comment="人工選的有進信件" if ok else f"漏掉：{output.get('原因') or 'AI 擋掉'}")
    ok = not output["進信件"]
    return Evaluation(name="過濾", value=1.0 if ok else 0.0,
                      comment="人工沒選，也沒進信件" if ok else "人工沒選，但進了信件")


def summary(*, item_results, **kwargs):
    from langfuse import Evaluation
    out = []
    for name in ("召回", "過濾"):
        vals = [e.value for r in item_results for e in r.evaluations if e.name == name]
        if vals:
            out.append(Evaluation(name=f"{name}率", value=sum(vals) / len(vals),
                                  comment=f"{sum(vals):.0f}/{len(vals)}"))
    return out


def cmd_eval(args):
    lf = tracing.client()
    if lf is None:
        raise SystemExit("Langfuse 未啟用（.env 缺 LANGFUSE_*）")
    result = lf.get_dataset(DATASET).run_experiment(
        name="系統 vs 人工篩選", description="候選檔目前狀態是否讓人工選的新聞進信件、擋掉沒選的",
        task=task, evaluators=[evaluator], run_evaluators=[summary], max_concurrency=8)
    tracing.flush()
    for e in result.run_evaluations:
        print(f"  全部 {e.name}：{e.value:.0%}（{e.comment}）")
    by_day = {}
    for r in result.item_results:
        d = by_day.setdefault(r.item.input["日期"], {"召回": [], "過濾": []})
        for e in r.evaluations:
            d[e.name].append(e.value)
    for day in sorted(by_day):
        cells = [f"{n}率 {sum(v) / len(v):.0%}（{sum(v):.0f}/{len(v)}）"
                 for n, v in by_day[day].items() if v]
        print(f"  {day}：" + "；".join(cells))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import", help="匯入某天的人工篩選結果")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("file", nargs="?", help="貼上內容的檔案；省略＝從標準輸入讀")
    p.add_argument("--replace", dest="add", action="store_false",
                   help="這天以這次貼上的為準（預設是與這天先前匯入的合併）")
    sub.add_parser("eval", help="在 Langfuse 比對系統與人工篩選")
    args = ap.parse_args()
    {"import": cmd_import, "eval": cmd_eval}[args.cmd](args)


if __name__ == "__main__":
    main()
