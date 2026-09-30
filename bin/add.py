# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""把 Google News 沒撈到的新聞用網址補進今天的候選清單。"""
import argparse, html as htmllib, re, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ROOT, TPE, clean_title, clean_url, find_existing, get,
                    load_clients, out_path, pretty_source, reporter_of,
                    url_key)


def title_of(page):
    for pat in (r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
                r'<meta[^>]+name="title"[^>]+content="([^"]+)"',
                r"<title[^>]*>(.*?)</title>"):
        m = re.search(pat, page, re.S | re.I)
        if m:
            return clean_title(re.sub(r"\s+", " ", m.group(1)))
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("client", help="客戶 id 或名稱")
    ap.add_argument("urls", nargs="+", help="一個或多個新聞網址")
    ap.add_argument("--date", help="要補進哪一天的候選清單，預設今天")
    args = ap.parse_args()

    cfg = load_clients(args.client)[0]
    day = (datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=TPE)
           if args.date else datetime.now(TPE))
    path = find_existing("candidates", cfg, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else \
        f"# {cfg['name']} 候選新聞 {day:%Y-%m-%d}（手動補件）\n"

    known = {url_key(u) for u in re.findall(r"https?://\S+", existing)}
    added = []
    for url in args.urls:
        url = clean_url(url)
        if url_key(url) in known:
            print(f"  · 已經在清單裡：{url}")
            continue
        try:
            page = get(url)
        except Exception as e:
            print(f"  ! 讀不到 {url}：{e}")
            page = ""
        src = pretty_source("", url, cfg.get("media_names"))
        who = reporter_of(url, src) if page else ""
        title = title_of(page) if page else ""
        known.add(url_key(url))
        added.append(f"- [x] {src}｜{who}｜{title}\n  {url}  <!-- 手動補 -->\n")
        print(f"  ＋ {src}｜{who}｜{title or '（標題請手動補）'}")

    if added:
        path.write_text(existing.rstrip("\n") + "\n\n" + "\n".join(added),
                        encoding="utf-8")
        print(f"✔ 已寫入 {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
