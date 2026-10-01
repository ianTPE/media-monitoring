# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "langfuse"]
# ///
"""撈當日候選新聞，產出可勾選的候選清單（candidates/）。

多客戶時走共用管線：關鍵字去重後只查一次 Google News，同一則新聞的網址還原
與記者名也只抓一次，再依關鍵字分派給各客戶，所以 20 家客戶不會打 20 輪。
"""
import argparse, hashlib, re, sys, time, urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ARTICLE_EXTRACT_VER, ROOT, TPE, clean_url, fetch_page,
                    google_news, is_holiday,
                    load_clients, load_state, out_path, article_text, pmap,
                    pretty_source, previous_workday, reporter_from_page,
                    resolve_url, save_state, url_key, url_rank, zh_day)
from workbook import read_searches
from commodity_judge import CommodityJudge


BROAD_LIMIT = 30


def norm(title):
    return re.sub(r"[\s　,，、。：:！!？?「」『』（）()\[\]【】\-－—…·.]+", "", title)


def search_terms(query, label):
    """取 Excel 搜尋式的實際主題詞，略過排除詞與泛用操作詞。"""
    positive = re.sub(r'-"[^"]+"|-\S+|\bsite:\S+', " ", query)
    quoted = re.findall(r'"([^"]+)"', positive)
    bare = re.sub(r'"[^"]+"', " ", positive)
    words = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9-]{2,}", bare)
    stop = {"AND", "OR", "NOT", "發展", "監管", "趨勢", "投資", "營收",
            "訂單", "合作", "法說", "展覽", "line", "today", "msn"}
    terms = {x.strip() for x in quoted + words + [label] if x.strip() and x.strip() not in stop}
    if label.endswith("供應鏈") and len(label) > 3:
        terms.add(label[:-3])
    return terms


def topic_hit(title, counts, terms):
    """Excel 延伸搜尋須在標題命中，或在正文出現至少兩次。"""
    return any(term.lower() in title.lower() or
               counts.get(term, 0) >= 2
               for term in terms)


def mentions(title, keys, prefix=True):
    """標題有沒有提到這些字。

    prefix=True 時長關鍵字可用前兩字比對（讓「某某生醫」對得上標題的「某某」），
    只用在公司名；產業關鍵字一律精確比對，否則「再生醫療」會縮成「再生」，
    連「都市再生創新企劃獎」都算命中。
    """
    for k in keys:
        if not k:
            continue
        cjk = re.fullmatch(r"[\u4e00-\u9fff]{2}", k[:2]) is not None
        if k in title or (prefix and cjk and len(k) >= 4 and k[:2] in title):
            return True
    return False


def core_of(cfg):
    return cfg.get("core_keywords") or \
        [cfg["name"]] + [q.split()[0] for q in (cfg.get("queries") or [])]


def topics_of(cfg):
    """產業關鍵字：不是自家新聞，但值得附在清單裡的主題（預設不勾選）。"""
    return cfg.get("topic_keywords") or []


def relevance_of(cfg):
    """off＝全部勾選；title＝標題沒提到就取消勾選；body＝再看內文，沒提到就不收。"""
    mode = cfg.get("relevance")
    if mode:
        return mode
    return "title" if cfg.get("uncheck_offtopic") else "off"


def window(today, cfg, args):
    """回傳 (起算時間, 說明)。預設從上一個上班日的 window_start（07:30，前一天最後一輪）起算：
    平常日只收前一天最後一輪之後的新聞，不跟昨天的信重複；
    週一會自動補上週五晚間與六、日，連假後也會自動往前涵蓋整個假期。"""
    midnight = {"hour": 0, "minute": 0, "second": 0, "microsecond": 0}
    if args.since:
        d = datetime.strptime(args.since, "%Y-%m-%d").date()
        return today.replace(year=d.year, month=d.month, day=d.day, **midnight), "（--since 指定）"
    if args.days:
        return (today - timedelta(days=args.days)).replace(**midnight), f"（--days {args.days}）"
    if cfg.get("lookback_days"):
        n = int(cfg["lookback_days"])
        return (today - timedelta(days=n)).replace(**midnight), f"（設定 {n} 天）"
    prev = previous_workday(today.date(), cfg)
    gap = (today.date() - prev).days
    off = [today.date() - timedelta(days=i) for i in range(1, gap)]
    off = [d for d in off if is_holiday(d, cfg)]
    hh, mm = (int(x) for x in str(cfg.get("window_start") or "00:00").split(":"))
    note = f"（上一個上班日 {zh_day(prev)} {hh:02d}:{mm:02d} 起"
    note += f"＋{len(off)} 天假日）" if off else "）"
    return today.replace(year=prev.year, month=prev.month, day=prev.day,
                         hour=hh, minute=mm, second=0, microsecond=0), note


def blocked_domain(url, cfg):
    host = urllib.parse.urlsplit(url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    for d in cfg.get("exclude_domains") or []:
        d = d.lower().lstrip(".")
        if host == d or host.endswith("." + d):   # 必須是網域邊界，否則 .mo 會命中 money.udn.com
            return True
    return False


def keep(it, cfg, cutoff):
    if it["when"] < cutoff:
        return False
    # exclude_unless_company 裡的字（如「庫藏股」）只在標題沒提到客戶時才排除：
    # 客戶自己宣布買回庫藏股是公司消息，「庫藏股一覽表」之類的盤勢整理才不收。
    unless = set(cfg.get("exclude_unless_company") or [])
    for x in cfg.get("exclude_keywords") or []:
        if x in it["title"]:
            if x in unless and mentions(it["title"], core_of(cfg),
                                        prefix=cfg.get("allow_core_prefix", True)):
                continue
            return False
    hay = f"{it['source']} {it['url']}".lower()
    if any(x.lower() in hay for x in cfg.get("exclude_sources") or []):
        return False
    if blocked_domain(it["url"], cfg):
        return False
    must = cfg.get("require_keywords") or []
    return not must or any(x in it["title"] for x in must)


def source_rank(src, cfg):
    order = cfg.get("source_order") or []
    return next((i for i, s in enumerate(order) if s in src), len(order))


def sort_key(it, cfg):
    return (source_rank(it["display"], cfg), it["when"])


def insert_sorted(lines, block, rank, cfg):
    """把新的一則插在第一個排序比它後面的媒體之前；人工調過的順序不動。"""
    starts = [i for i, l in enumerate(lines) if re.match(r"- \[[ xX]\] ", l)]
    for i in starts:
        if source_rank(lines[i][6:].split("｜", 1)[0], cfg) > rank:
            lines[i:i] = block
            return
    if not starts:
        lines += [""] + block if lines and lines[-1] else block
        return
    j = starts[-1] + 1
    while j < len(lines) and lines[j].startswith("  "):
        j += 1
    if j < len(lines) and not lines[j]:
        j += 1
    lines[j:j] = block if not lines[j - 1] else [""] + block


def gather(qmap, qdays, workers, base_map, book_map):
    """關鍵字去重後平行查 Google News；保留每家客戶的搜尋來源。"""
    def one(q):
        error = None
        for attempt in range(3):
            try:
                return q, google_news(q, qdays[q]), False
            except Exception as e:
                error = e
                if attempt < 2:
                    time.sleep(attempt + 1)
        print(f"  ! 查詢「{q}」失敗：{error}", file=sys.stderr)
        return q, [], True

    pool, raw, failed = {}, 0, 0
    for q, results, error in pmap(one, list(qmap), workers):
        failed += error
        # 指定網站搜尋（例：公司名 site:ctee.com.tw）不再要求標題有公司名：族群報導常只在
        # 內文提到客戶（2026-09-30 漏了工商〈新藥股旺到2027年〉）。這些結果走 Excel 延伸搜尋
        # 的關聯判斷，標題或內文要提到主題詞才留下，預設不勾。
        # 產業詞（Excel／extra_searches）只取前一批，避免候選清單被泛新聞淹沒。
        # 原本 12 則會截掉排 13～20 名的相關報導（2026-09-29 人工比對），放寬到 30。
        items = results if q in base_map else results[:BROAD_LIMIT]
        for it in items:
            raw += 1
            key = (norm(it["title"]), it["source"])
            cur = pool.setdefault(key, {**it, "clients": set(),
                                        "base_clients": set(), "book_hits": {}})
            cur["clients"] |= qmap[q]
            cur["base_clients"] |= base_map.get(q, set())
            for cid, label in book_map.get(q, {}).items():
                cur["book_hits"].setdefault(cid, set()).add(label)
            if it["when"] < cur["when"]:
                cur["when"] = it["when"]
    return pool, raw, failed


def write_overview(clients, stats, today, cfg0):
    tpl = (cfg0.get("paths") or {}).get("overview") or "_總覽/{date} 候選總覽.md"
    path = ROOT / "candidates" / tpl.format(date=f"{today:%Y-%m-%d}", ym=f"{today:%Y-%m}")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {today:%Y-%m-%d} 候選總覽", "",
             f"產生時間：{today:%H:%M}　客戶 {len(clients)} 家", "",
             "| 客戶 | 候選 | 預設勾選 | 涵蓋範圍 | 清單 |", "|---|---|---|---|---|"]
    ordered = sorted(clients, key=lambda c: (-stats[c["id"]]["checked"],
                                             -stats[c["id"]]["total"], c["name"]))
    for cfg in ordered:
        s = stats[cfg["id"]]
        lines.append(f"| {cfg['name']} | {s['total']} | {s['checked']} | "
                     f"{s['from']} 起 | [[{s['stem']}]] |")
    zero = [c["name"] for c in clients if stats[c["id"]]["total"] == 0]
    if zero:
        lines += ["", f"> 今天沒有候選新聞的客戶：{'、'.join(zero)}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("client", nargs="?", help="客戶 id 或名稱，省略＝全部")
    ap.add_argument("--days", type=int, help="往回撈幾天（預設自動算到上一個上班日）")
    ap.add_argument("--since", help="從哪一天 00:00 起算 YYYY-MM-DD")
    ap.add_argument("--date", help="報告日期 YYYY-MM-DD，預設今天")
    ap.add_argument("--no-reporter", action="store_true", help="不抓記者名（比較快）")
    ap.add_argument("--workers", type=int, default=8, help="平行連線數，預設 8")
    ap.add_argument("--workbook", help="Excel 搜尋清單；預設讀 defaults.yaml 的 search_workbook")
    ap.add_argument("--no-workbook", action="store_true", help="只用 clients/*.yaml 搜尋")
    ap.add_argument("--overwrite", action="store_true", help="覆寫已存在的候選檔（會清除人工編輯）")
    ap.add_argument("--merge", action="store_true",
                    help="候選檔已存在時，只把新撈到的新聞依媒體排序補進去，保留人工編輯")
    args = ap.parse_args()
    if args.merge and args.overwrite:
        ap.error("--merge 與 --overwrite 不能同時使用")

    today = (datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=TPE)
             if args.date else datetime.now(TPE))
    clients = load_clients(args.client)
    wins = {c["id"]: window(today, c, args) for c in clients}

    qmap, base_map, book_map, watch_terms = {}, {}, {}, {}
    for c in clients:
        for q in c.get("queries") or [c["name"]]:
            qmap.setdefault(q, set()).add(c["id"])
            base_map.setdefault(q, set()).add(c["id"])

    workbook_path = None if args.no_workbook else (args.workbook or clients[0].get("search_workbook"))
    if workbook_path:
        from pathlib import Path
        path = Path(workbook_path).expanduser()
        if not path.is_file():
            ap.error(f"找不到 Excel 搜尋清單：{path}（或加 --no-workbook）")
        searches = read_searches(path)
        aliases = clients[0].get("workbook_aliases") or {}   # 寫在 defaults.local.yaml
        names = {c["name"]: c["id"] for c in clients}
        used = set()
        for name, entries in searches.items():
            cid = names.get(aliases.get(name, name))
            if not cid:
                continue
            used.add(name)
            for entry in entries:
                q, label = entry["query"], entry["label"]
                qmap.setdefault(q, set()).add(cid)
                book_map.setdefault(q, {})[cid] = label
                watch_terms.setdefault((cid, label), set()).update(search_terms(q, label))
        missing = sorted(set(searches) - used)
        print(f"  Excel 搜尋清單：{len(used)} 家客戶、{sum(len(v) for v in book_map.values())} 組搜尋")
        if missing and not args.client:
            print(f"  注意：Excel 另有未設定客戶的搜尋：{'、'.join(missing)}")
    # 客戶檔的 extra_searches：和 Excel 產業搜尋同樣處理（預設不勾、須命中主題詞）。
    extra_labels = set()
    for c in clients:
        for entry in c.get("extra_searches") or []:
            if isinstance(entry, str):
                entry = {"query": entry, "label": entry.split()[0]}
            q, label = entry["query"], entry["label"]
            qmap.setdefault(q, set()).add(c["id"])
            book_map.setdefault(q, {})[c["id"]] = label
            watch_terms.setdefault((c["id"], label), set()).update(search_terms(q, label))
            extra_labels.add((c["id"], label))
    qdays = {q: max((today.date() - wins[cid][0].date()).days + 1 for cid in ids)
             for q, ids in qmap.items()}

    print(f"▶ {len(clients)} 家客戶、{len(qmap)} 組關鍵字（去重後）")
    pool, raw, failed = gather(qmap, qdays, args.workers, base_map, book_map)
    # 少數幾組失敗（Google 偶發 404）照樣出清單，下一輪 --merge 會補回；
    # 大量失敗多半是斷網或被擋，才整輪放棄。2026-09-30 02:00 曾因 1/168 組失敗整輪沒跑。
    if failed > max(3, len(qmap) // 20):
        raise SystemExit(f"{failed}/{len(qmap)} 組搜尋失敗；沒有改寫候選檔。請稍後重試。")
    if failed:
        print(f"  ⚠ {failed}/{len(qmap)} 組搜尋失敗，其餘照常處理；下一輪會再補", file=sys.stderr)
    print(f"  Google News 回傳 {raw} 則 → 標題去重 {len(pool)} 則，還原網址中…")

    urlcache = load_state("urlcache.json", {})
    before = len(urlcache)
    items = list(pool.values())
    for it, real in zip(items, pmap(lambda x: resolve_url(x["gurl"], urlcache),
                                    items, args.workers)):
        it["url"] = clean_url(real)
    save_state("urlcache.json", urlcache)
    print(f"  網址還原完成（新抓 {len(urlcache) - before} 則，其餘走快取）")

    # 同一篇報導可能以 AMP 版、別的語系子網域或不同網址寫法出現，合併成一則。
    merged = {}
    for it in items:
        key = url_key(it["url"])
        cur = merged.get(key)
        if cur:
            if url_rank(it["url"]) < url_rank(cur["url"]):
                it, cur = cur, it
                merged[key] = cur
            cur["clients"] |= it["clients"]
            cur["base_clients"] |= it["base_clients"]
            for cid, labels in it["book_hits"].items():
                cur["book_hits"].setdefault(cid, set()).update(labels)
            cur["when"] = min(cur["when"], it["when"])
        else:
            merged[key] = it
    items = list(merged.values())

    picked = {}
    for cfg in clients:
        cutoff = wins[cfg["id"]][0]
        rows = [it for it in items if cfg["id"] in it["clients"] and keep(it, cfg, cutoff)]
        picked[cfg["id"]] = rows

    rcache = load_state("reporters.json", {})
    bcache = load_state("bodyhits.json", {})
    universe = sorted({k for cfg in clients for k in core_of(cfg) + topics_of(cfg)}
                      | {k for terms in watch_terms.values() for k in terms})
    uhash = hashlib.sha1(("|".join(universe) + ARTICLE_EXTRACT_VER)
                         .encode()).hexdigest()[:8]

    srcs = {it["url"]: it["source"] for rows in picked.values() for it in rows}
    need_reporter = set() if args.no_reporter else {
        u for u in srcs if u not in rcache}
    need_body = set()
    for cfg in clients:
        core = core_of(cfg)
        core_prefix = cfg.get("allow_core_prefix", True)
        for it in picked[cfg["id"]]:
            if mentions(it["title"], core, prefix=core_prefix):
                continue
            book_only = (cfg["id"] in it["book_hits"]
                         and cfg["id"] not in it["base_clients"])
            if relevance_of(cfg) != "body" and not book_only:
                continue
            if book_only:
                terms = {term for label in it["book_hits"][cfg["id"]]
                         for term in watch_terms.get((cfg["id"], label), ())}
                if topic_hit(it["title"], {}, terms):
                    continue
            hit = bcache.get(it["url"])
            if not hit or hit.get("u") != uhash:
                need_body.add(it["url"])

    todo = sorted(need_reporter | need_body)
    if todo:
        print(f"  讀取 {len(todo)} 篇內文（記者名 {len(need_reporter)}、"
              f"關聯度 {len(need_body)}；其餘走快取）…")
        for url, page in zip(todo, pmap(fetch_page, todo, args.workers)):
            if url in need_reporter:
                rcache[url] = {"n": reporter_from_page(page, srcs.get(url, "")),
                               "d": f"{today:%Y-%m-%d}"}
            if url in need_body:
                text = article_text(page)
                bcache[url] = {"c": {k: text.count(k) for k in universe
                                     if k and k in text},
                               "u": uhash, "d": f"{today:%Y-%m-%d}"}
        keep_after = (today - timedelta(days=90)).strftime("%Y-%m-%d")
        rcache = {u: v for u, v in rcache.items() if v.get("d", "") >= keep_after}
        bcache = {u: v for u, v in bcache.items() if v.get("d", "") >= keep_after}
        save_state("reporters.json", rcache)
        save_state("bodyhits.json", bcache)

    stats, unmapped = {}, set()
    # 每天每家寫進候選檔過的新聞；--merge 靠它避免把人工刪掉的又補回來。
    seen_all = load_state("candidates-seen.json", {})
    day_key = f"{today:%Y-%m-%d}"
    seen_day = seen_all.setdefault(day_key, {})
    commodity_judge = CommodityJudge(today, args.workers)
    for cfg in clients:
        cutoff, note = wins[cfg["id"]]
        rows = picked[cfg["id"]]
        for it in rows:
            it["display"] = pretty_source(it["source"], it["url"], cfg.get("media_names"))
        rows = sorted(rows, key=lambda r: sort_key(r, cfg))
        sent = load_state(f"sent-{cfg['id']}.json", {})
        sent_keys = {url_key(u): d for u, d in sent.items()}
        core = core_of(cfg)
        core_prefix = cfg.get("allow_core_prefix", True)
        topics = topics_of(cfg)
        mode = relevance_of(cfg)
        syn = cfg.get("syndicated_sources") or []
        dropped = 0

        out = out_path("candidates", cfg, today, make_dir=True)
        merging = args.merge and out.exists()
        if out.exists() and not args.overwrite and not merging:
            old = out.read_text(encoding="utf-8")
            shown = len(re.findall(r"(?m)^- \[[ xX]\] ", old))
            checked = len(re.findall(r"(?m)^- \[[xX]\] ", old))
            stats[cfg["id"]] = {"total": shown, "checked": checked,
                                "from": zh_day(cutoff), "stem": out.stem}
            print(f"  · {cfg['name']}：候選檔已存在，保留人工編輯（{shown} 則）")
            continue
        head_n = len(rows)
        lines = [f"# {cfg['name']} 候選新聞 {today:%Y-%m-%d}（{{n}} 則）",
                 f"涵蓋範圍：{zh_day(cutoff)} {cutoff:%H:%M} ～ {zh_day(today)} {today:%H:%M}", "",
                 "> 勾選規則：`- [x]` 會進報告，不要的整行刪掉或把 x 拿掉。",
                 "> 欄位是 `媒體｜記者｜標題`，下一行是網址；記者空白請手動補。",
                 "> 順序可自行搬動，報告會照這個順序輸出。",
                 f"> 編輯完存檔後執行： ./monitor build {cfg['id']}", ""]
        checked = 0
        entries = []
        one_per = set(cfg.get("one_per_topic") or [])
        reps = []           # 行情類主題（銀價、銅價…）的候選，迴圈後每主題只挑一則
        for idx, it in enumerate(rows):
            book_only = (cfg["id"] in it["book_hits"]
                         and cfg["id"] not in it["base_clients"])
            counts = (bcache.get(it["url"]) or {}).get("c") or {}
            if book_only and not mentions(it["title"], core, prefix=core_prefix):
                terms = {term for label in it["book_hits"][cfg["id"]]
                         for term in watch_terms.get((cfg["id"], label), ())}
                # 主題詞要在標題或內文出現兩次；但內文提到客戶公司名本身，一次就算
                # （2026-09-30 一則工商族群報導只抓到前段，公司名只出現一次）
                if not topic_hit(it["title"], counts, terms) and not (set(counts) & set(core)):
                    dropped += 1
                    continue
            offtopic = ((mode != "off" or book_only)
                        and not mentions(it["title"], core, prefix=core_prefix))
            why = ""
            limited = set()
            if offtopic:
                hits = set(counts)
                if mode == "body" and hits & set(core):
                    kw = max(hits & set(core), key=lambda k: counts[k])
                    why = f"內文提到 {kw}×{counts[kw]}"
                elif topics and (mentions(it["title"], topics, prefix=False)
                                 or hits & set(topics)):
                    tp = hits & set(topics)
                    why = (f"產業新聞：{max(tp, key=lambda k: counts[k])}"
                           f"×{max(counts[k] for k in tp)}" if tp else "產業新聞（標題）")
                elif book_only:
                    labels = sorted(it["book_hits"][cfg["id"]])
                    kind = ("產業搜尋" if all((cfg["id"], l) in extra_labels for l in labels)
                            else "Excel 搜尋")
                    why = f"{kind}：{'、'.join(labels[:3])}"
                    if one_per and set(labels) <= one_per:
                        limited = set(labels)
                        why += "（同主題只留一則）"
                elif mode == "body":
                    dropped += 1
                    continue
                else:
                    why = "標題未提到關鍵字"
            src = it["display"] + (" (轉)" if any(s in it["source"] for s in syn) else "")
            if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", it["display"] or ""):
                unmapped.add(it["display"])
            stamp = f"{it['when']:%m/%d %H:%M}"
            sent_on = sent.get(it["url"]) or sent_keys.get(url_key(it["url"]))
            if sent_on:
                mark, note2 = " ", f"  <!-- {stamp}｜{sent_on} 已送過 -->"
            elif offtopic:
                if cfg.get("check_offtopic"):
                    mark, note2 = "x", f"  <!-- {stamp}｜{why} -->"
                else:
                    mark, note2 = " ", f"  <!-- {stamp}｜{why}，確認後再勾 -->"
            else:
                mark, note2 = "x", f"  <!-- {stamp} -->"
            who = rcache.get(it["url"], {}).get("n", "")
            entry = (url_key(it["url"]), mark, source_rank(src, cfg), [
                f"- [{mark}] {src}｜{who}｜{it['title']}", f"  {it['url']}{note2}", ""])
            if limited:
                reps.append((idx, entry, limited, it))
            else:
                entries.append((idx, entry))

        seen = set(seen_day.get(cfg["id"], [])) if args.merge else set()
        # 只看候選檔裡現在還在的代表：代表被刪掉（人工或清理）後，下一輪可以補另一則；
        # 被刪的那一則本身仍記在 seen 裡，不會再加回來。（2026-09-30 銅價代表被刪後整天沒有銅價新聞）
        covered = set()
        if merging and out.exists():
            old = out.read_text(encoding="utf-8")
            seen |= {url_key(u) for u in re.findall(r"https?://[^\s<>]+", old)}
            for note_labels in re.findall(r"搜尋：([^，（>]+)", old):
                covered |= set(note_labels.split("、")) & one_per
        selected = set()
        ranked = sorted(reps, key=lambda r: (r[1][2], -r[3]["when"].timestamp()))
        for topic in sorted(one_per - covered):
            if topic in covered:
                continue
            choices = [r for r in ranked if topic in r[2] and r[1][0] not in seen]
            if not choices:
                continue
            chosen_url = commodity_judge.choose(topic, [r[3] for r in choices])
            winner = next((r for r in choices if r[3]["url"] == chosen_url), None)
            if winner is not None:
                covered |= winner[2]
                if winner[1][0] not in selected:
                    entries.append((winner[0], winner[1]))
                    selected.add(winner[1][0])
        skipped = len(reps) - len(selected)
        dropped += skipped
        entries = [e for _, e in sorted(entries, key=lambda x: x[0])]
        topic_marks = {f"topic:{l}" for l in covered}
        if merging:
            new = [e for e in entries if e[0] not in seen]
            if new:
                body = old.rstrip("\n").split("\n")
                for _, _, rank, block in new:
                    block[1] = block[1].replace("  <!-- ", f"  <!-- {today:%H:%M} 補進｜", 1)
                    insert_sorted(body, block, rank, cfg)
                old = "\n".join(body)
            old = re.sub(r"(～ .+?) \d\d:\d\d$", rf"\1 {today:%H:%M}", old, count=1, flags=re.M)
            shown = len(re.findall(r"(?m)^- \[[ xX]\] ", old))
            checked = len(re.findall(r"(?m)^- \[[xX]\] ", old))
            old = re.sub(r"^(# .+?（)\d+( 則）)", rf"\g<1>{shown}\2", old, count=1)
            out.write_text(old.rstrip("\n") + "\n", encoding="utf-8")
            seen_day[cfg["id"]] = sorted(seen | {e[0] for e in new} | topic_marks)
            stats[cfg["id"]] = {"total": shown, "checked": checked,
                                "from": zh_day(cutoff), "stem": out.stem}
            print(f"  ＋ {cfg['name']}：補進 {len(new)} 則，共 {shown} 則（勾選 {checked}）")
            continue

        for _, mark, _, block in entries:
            checked += mark == "x"
            lines += block
        lines[0] = lines[0].format(n=len(rows) - dropped)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        seen_day[cfg["id"]] = sorted({e[0] for e in entries} | topic_marks)
        shown = len(rows) - dropped
        stats[cfg["id"]] = {"total": shown, "checked": checked,
                            "from": zh_day(cutoff), "stem": out.stem}
        tail = f"，濾掉 {dropped - skipped} 則關聯度不足的" if dropped - skipped else ""
        tail += f"，行情同主題略過 {skipped} 則" if skipped else ""
        print(f"  ✔ {cfg['name']}：{shown} 則（勾選 {checked}{tail}）{note}")

    keep_after = f"{today - timedelta(days=14):%Y-%m-%d}"
    save_state("candidates-seen.json",
               {d: v for d, v in seen_all.items() if d >= keep_after})
    if len(clients) > 1:
        ov = write_overview(clients, stats, today, clients[0])
        print(f"  ✔ 總覽：{ov.relative_to(ROOT)}")
    if unmapped:
        print(f"  ⚠ 這些來源還沒有中文名，可加進 defaults.yaml 的 media_names："
              f"{'、'.join(sorted(unmapped))}")


if __name__ == "__main__":
    try:
        main()
    finally:
        import tracing
        tracing.flush()   # 把挑行情代表的 AI 紀錄送到 Langfuse
