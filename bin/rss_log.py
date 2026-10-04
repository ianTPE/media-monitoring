# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "langfuse"]
# ///
"""試驗：各家媒體自己的 RSS 能不能補到 Google News 漏掉的新聞（2026-10-04 起記錄一週）。

只記錄，不寫進候選檔：

  ./monitor rss poll      抓一次所有 feed，新出現的存進 .state/rss-log/（crontab 每 10 分鐘）
  ./monitor rss report    人工挑選裡，有幾則出現在 RSS、卻不在系統候選裡

背景：9/29～10/01 人工選的 53 則有 15 則沒撈到，其中工商〈新藥股旺到2027年〉是 Google
一直沒收錄原文。各家 RSS 只給最新 15～40 則，工商 03:00 會整批上架報紙版，所以每輪也記下
「這次抓到的全是新的」——那代表 feed 太短、兩輪之間可能有漏。

另外每小時（整點那輪）拿客戶的查詢詞搜 Yahoo 奇摩新聞和鉅亨網站內搜尋。公司規定報告只能用
原始出處網址：Yahoo 多半是轉載，頁面上只有原始媒體名稱、沒有原文網址，所以 Yahoo 只當
「發現來源」，report 會分開列出轉載的那幾則找不找得到原文。
"""

import argparse
import fcntl
import html
import json
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, TPE, clean_url, get, load_clients, url_key

UNTIL = date(2026, 10, 11)      # 記錄到這天為止，之後 poll 自動不做事
LOG = ROOT / ".state" / "rss-log"

FEEDS = [("工商時報", f"https://www.ctee.com.tw/rss_web/livenews/{c}")
         for c in ("ctee", "industry", "stock", "finance", "tech", "policy", "world")]
FEEDS += [("經濟日報", f"https://money.udn.com/rssfeed/news/1001/{c}")
          for c in (5588, 5589, 5590, 5591, 5592, 5593, 5595, 5596, 5597, 10846, 11111, 12017)]
FEEDS += [("鉅亨網", f"cnyes:{c}") for c in ("headline", "tw_stock", "wd_stock")]
FEEDS += [
    ("MoneyDJ", "https://www.moneydj.com/KMDJ/RssCenter.aspx"),
    ("中央社", "https://feeds.feedburner.com/rsscna/finance"),
    ("中央社", "https://feeds.feedburner.com/rsscna/technology"),
    ("自由時報", "https://news.ltn.com.tw/rss/business.xml"),
    ("ETtoday", "https://feeds.feedburner.com/ettoday/finance"),
    ("科技新報", "https://technews.tw/feed/"),
]
CNYES_PAGES = 5     # 鉅亨可以翻頁：翻到上一輪看過的為止，最多 5 頁（150 則）
SEARCH_DAYS = 3     # 搜尋結果只記最近幾天的，免得第一次把舊聞全灌進來
YAHOO_OWN = ("Yahoo",)      # Yahoo 自己記者寫的（Yahoo股市、Yahoo奇摩新聞）算原文


def rss_items(url):
    root = ET.fromstring(get(url, timeout=20).encode("utf-8"))
    out = []
    for it in root.iter("item"):
        when = None
        try:
            when = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(TPE)
        except Exception:
            pass
        out.append({"title": (it.findtext("title") or "").strip(),
                    "url": (it.findtext("link") or "").strip(), "when": when})
    return out


def cnyes_items(category, seen):
    out = []
    for page in range(1, CNYES_PAGES + 1):
        raw = json.loads(get(f"https://api.cnyes.com/media/api/v1/newslist/category/"
                             f"{category}?limit=30&page={page}", timeout=20))
        batch = [{"title": d["title"], "url": f"https://news.cnyes.com/news/id/{d['newsId']}",
                  "when": datetime.fromtimestamp(d["publishAt"], TPE)}
                 for d in raw["items"]["data"]]
        out += batch
        if not batch or any(url_key(b["url"]) in seen for b in batch):
            break
    return out


def search_queries():
    return sorted({q for c in load_clients() for q in c.get("queries") or []})


def _ago(text, now):
    """Yahoo 只給「3 小時前」「2 週前」「1 個月前」或「2026年9月3日」，換成大概的時間。"""
    m = re.search(r"(\d+)\s*(分鐘|小時|天|週|個月|年)前", text)
    if m:
        days = {"分鐘": 1 / 1440, "小時": 1 / 24, "天": 1, "週": 7, "個月": 30, "年": 365}[m.group(2)]
        return now - timedelta(days=int(m.group(1)) * days)
    m = re.search(r"(\d{4})年(\d+)月(\d+)日", text)
    if m:
        return datetime(*map(int, m.groups()), tzinfo=TPE)
    return None


def yahoo_items(q, now):
    page = get("https://tw.news.yahoo.com/search?p=" + urllib.parse.quote(q), timeout=20)
    out = []
    for m in re.finditer(r'<h3[^>]*><a data-ylk="[^"]*sec:related_news;[^"]*"[^>]*href="([^"]+)"[^>]*>'
                         r'(.*?)</a></h3>(.{0,3000}?)<div class="text-px12[^"]*">(.*?)</div>', page, re.S):
        url, title, _, meta = m.groups()
        provider = html.unescape(re.sub(r"<[^>]+>", "", meta)).split("・")[0].strip()
        out.append({"title": html.unescape(re.sub(r"<[^>]+>", "", title)).strip(),
                    "url": html.unescape(url), "when": _ago(meta, now), "provider": provider,
                    "reprint": not provider.startswith(YAHOO_OWN)})
    return out


def cnyes_search(q):
    raw = json.loads(get("https://api.cnyes.com/media/api/v1/search?limit=20&q="
                         + urllib.parse.quote(q), timeout=20))
    return [{"title": re.sub(r"</?mark>", "", d["title"]),
             "url": f"https://news.cnyes.com/news/id/{d['newsId']}",
             "when": datetime.fromtimestamp(d["publishAt"], TPE)}
            for d in raw["items"]["data"]]


def cmd_poll(args):
    now = datetime.now(TPE)
    if now.date() > UNTIL:
        return
    LOG.mkdir(parents=True, exist_ok=True)
    # 整點那輪要搜尋，跑兩三分鐘；上一輪還沒跑完就跳過，免得兩邊同時改 seen.json
    lock = open(LOG / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"{now:%m-%d %H:%M} 上一輪還在跑，跳過")
        return
    seen_path = LOG / "seen.json"
    seen = json.loads(seen_path.read_text(encoding="utf-8")) if seen_path.exists() else {}
    stats, new_rows = [], []
    sources = list(FEEDS)
    if args.search or now.minute < 10:      # 搜尋每小時一次就好，別一直敲人家
        qs = search_queries()
        sources += [("Yahoo搜尋", f"yahoo:{q}") for q in qs]
        sources += [("鉅亨網", f"cnyes-search:{q}") for q in qs]
    for media, url in sources:
        kind, _, arg = url.partition(":")
        search = kind in ("yahoo", "cnyes-search")
        try:
            items = (cnyes_items(arg, seen) if kind == "cnyes"
                     else yahoo_items(arg, now) if kind == "yahoo"
                     else cnyes_search(arg) if kind == "cnyes-search"
                     else rss_items(url))
        except Exception as e:
            stats.append({"feed": url, "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        if search:
            items = [it for it in items
                     if not it["when"] or it["when"] >= now - timedelta(days=SEARCH_DAYS)]
        fresh = 0
        for it in items:
            if not it["url"]:
                continue
            k = url_key(clean_url(it["url"]))
            if k in seen:
                continue
            fresh += 1
            seen[k] = now.isoformat(timespec="minutes")
            new_rows.append({"media": media, "feed": url, "title": it["title"],
                             "url": clean_url(it["url"]),
                             "published": it["when"].isoformat(timespec="minutes") if it["when"] else "",
                             "first_seen": seen[k]})
            if "provider" in it:
                new_rows[-1].update(provider=it["provider"], reprint=it["reprint"])
        # 第一次跑全是新的是正常的；之後還全是新的，代表兩輪之間 feed 已經被洗過一輪
        # （搜尋結果本來就只給前 20 則，不算溢出）
        stats.append({"feed": url, "items": len(items), "new": fresh,
                      "overflow": bool(items) and fresh == len(items) and args.warm and not search})
        time.sleep(0.5)
    with open(LOG / f"{now:%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
        for row in new_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(LOG / "polls.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now.isoformat(timespec="minutes"), "feeds": stats},
                           ensure_ascii=False) + "\n")
    keep_after = (now - timedelta(days=14)).isoformat()
    seen_path.write_text(json.dumps({k: v for k, v in seen.items() if v >= keep_after},
                                    ensure_ascii=False), encoding="utf-8")
    errors = [s for s in stats if "error" in s]
    print(f"{now:%m-%d %H:%M} 新增 {len(new_rows)} 則；"
          f"feed 失敗 {len(errors)}；可能溢出 {sum(s.get('overflow', False) for s in stats)}")


def cmd_report(args):
    from picks import candidates_for, find, key_of, norm_title
    rows = []
    for p in sorted(LOG.glob("2*.jsonl")):
        rows += [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l]
    if not rows:
        raise SystemExit("還沒有 RSS 紀錄")
    by_key = {key_of(r["url"]): r for r in rows}
    # 同標題有原文也有轉載時，原文優先
    by_title = {norm_title(r["title"]): r for r in sorted(rows, key=lambda r: not r.get("reprint"))
                if norm_title(r["title"])}
    start = min(r["first_seen"] for r in rows)[:10]
    cfgs = {c["id"]: c for c in load_clients()}
    total = in_cands = 0
    rescued, reprint_only, missing = [], [], []
    for f in sorted((ROOT / ".state" / "picks").glob("*.json")):
        day = date.fromisoformat(f.stem)
        if str(day) < start:
            continue
        picks = json.loads(f.read_text(encoding="utf-8"))["picks"]
        for cid, items in picks.items():
            cands = candidates_for(cfgs[cid], day) or [] if cid in cfgs else []
            for p in items:
                total += 1
                if find(cands, p["url"], p["title"]):
                    in_cands += 1
                    continue
                hit = by_key.get(key_of(p["url"])) or by_title.get(norm_title(p["title"]))
                line = f"{day} {cid}｜{p['source']}｜{p['title'][:36]}"
                if hit and hit.get("reprint"):
                    # 只在 Yahoo 轉載看到：報告不能用轉載網址，要另外找得到原文才算補回
                    reprint_only.append(f"{line}\n      Yahoo 轉載自 {hit['provider']}，"
                                        f"首次看到 {hit['first_seen'][5:16]}；記錄裡沒有原文網址")
                elif hit:
                    src = hit["media"] + (f"（{hit['provider']}）" if hit.get("provider") else "")
                    rescued.append(f"{line}\n      {src} 首次看到 {hit['first_seen'][5:16]}"
                                   f"（刊出 {hit['published'][5:16] or '?'}）")
                else:
                    missing.append(line)
    print(f"RSS 紀錄從 {start} 起，共 {len(rows)} 則")
    print(f"期間人工選了 {total} 則：系統候選有 {in_cands}；"
          f"候選沒有但記錄有原文 {len(rescued)}；只有 Yahoo 轉載 {len(reprint_only)}；"
          f"都沒有 {len(missing)}")
    for x in rescued:
        print("  ＋", x)
    for x in reprint_only:
        print("  △", x)
    for x in missing:
        print("  ✗", x)
    polls = [json.loads(l) for l in (LOG / "polls.jsonl").read_text(encoding="utf-8").splitlines() if l]
    over, errs = {}, {}
    for p in polls:
        for s in p["feeds"]:
            over[s["feed"]] = over.get(s["feed"], 0) + bool(s.get("overflow"))
            errs[s["feed"]] = errs.get(s["feed"], 0) + ("error" in s)
    print(f"\n{len(polls)} 輪抓取；可能溢出（兩輪之間 feed 被洗過）或失敗的 feed：")
    for feed in over:
        if over[feed] or errs[feed]:
            print(f"  {feed}：溢出 {over[feed]} 次、失敗 {errs[feed]} 次")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    poll = sub.add_parser("poll")
    poll.add_argument("--cold", dest="warm", action="store_false",
                      help="第一次跑：全是新的不算溢出")
    poll.add_argument("--search", action="store_true",
                      help="不是整點也跑 Yahoo／鉅亨搜尋")
    sub.add_parser("report")
    args = ap.parse_args()
    if args.cmd == "poll" and args.warm and not (LOG / "seen.json").exists():
        args.warm = False
    {"poll": cmd_poll, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
