# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""把勾選好的候選清單排版成可直接貼給客戶的監測報告（reports/）。"""
import argparse, re, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ROOT, TPE, WEEK_ZH, find_existing, load_clients, load_state,
                    out_path, save_state)

LINE = re.compile(r"^- \[([ xX])\]\s*(.+?)\s*$")


def parse(path):
    picked = []
    cur = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = LINE.match(raw)
        if m:
            cur = None
            if m.group(1).lower() != "x":
                continue
            parts = [p.strip() for p in m.group(2).split("｜")]
            parts += [""] * (3 - len(parts))
            cur = {"source": parts[0], "reporter": parts[1], "title": parts[2],
                   "url": ""}
            picked.append(cur)
        elif cur is not None:
            u = re.match(r"\s*(https?://\S+)", raw)
            if u:
                cur["url"] = u.group(1)
                cur = None
    return [p for p in picked if p["title"]]


def render(cfg, items, day):
    tpl = cfg.get("report_title", "{name}【 {m}/{d} 新聞監測】")
    head = tpl.format(name=cfg["name"], m=day.month, d=day.day,
                      date=day.strftime("%Y-%m-%d"))
    numbered = cfg.get("numbered", True)
    out = [head, ""]
    for i, it in enumerate(items, 1):
        who = f" {it['reporter']}" if it["reporter"] else ""
        no = f"{i}." if numbered else ""
        out += [f"{no}{it['source']}{who}", it["title"], it["url"], ""]
    return "\n".join(out).rstrip() + "\n"


def write_index(cfg):
    """每個客戶一份索引，長期要回頭查哪天發了什麼就看這份。"""
    base = ROOT / "reports"
    pat = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")
    found = []
    for f in base.rglob("*.md"):
        m = pat.match(f.stem)
        if not m or cfg["id"] not in f.stem:
            continue
        n = sum(1 for ln in f.read_text(encoding="utf-8").splitlines()
                if ln.startswith("http"))
        found.append((m.group(1), n, f))
    if not found:
        return None
    found.sort(reverse=True)
    home = base / cfg["id"]
    out = (home if home.is_dir() else base) / f"{cfg['name']}報告索引.md"

    lines = [f"# {cfg['name']} 監測報告索引", "",
             f"更新：{datetime.now(TPE):%Y-%m-%d %H:%M}　共 {len(found)} 份"]
    month = None
    for date_str, n, f in found:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        if date_str[:7] != month:
            month = date_str[:7]
            lines += ["", f"## {month}", "", "| 日期 | 則數 | 報告 |", "|---|---|---|"]
        lines.append(f"| {d:%m-%d}（{WEEK_ZH[d.weekday()]}） | {n} | [[{f.stem}]] |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("client", nargs="?", help="客戶 id 或名稱，省略＝全部")
    ap.add_argument("--date", help="報告日期 YYYY-MM-DD，預設今天")
    ap.add_argument("--file", help="指定候選檔路徑")
    ap.add_argument("--print", "-p", dest="show", action="store_true",
                    help="把報告全文印出來（單一客戶時預設就會印）")
    args = ap.parse_args()

    day = (datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=TPE)
           if args.date else datetime.now(TPE))
    date_str = day.strftime("%Y-%m-%d")

    clients = load_clients(args.client)
    show = args.show or len(clients) == 1
    done, empty = [], []
    for cfg in clients:
        src = Path(args.file) if args.file else find_existing("candidates", cfg, day)
        if not src.exists():
            empty.append((cfg["name"], "沒有候選檔，先跑 fetch"))
            continue
        items = parse(src)
        if not items:
            empty.append((cfg["name"], "沒有勾選的項目"))
            continue
        text = render(cfg, items, day)
        out = out_path("reports", cfg, day, make_dir=True)
        out.write_text(text, encoding="utf-8")

        sent = load_state(f"sent-{cfg['id']}.json", {})
        for it in items:
            if it["url"]:
                sent[it["url"]] = date_str
        save_state(f"sent-{cfg['id']}.json", sent)

        idx = write_index(cfg)
        missing = [it["title"] for it in items if not it["reporter"]]
        done.append((cfg["name"], len(items), len(missing), out))
        print(f"✔ {cfg['name']}：{len(items)} 則 → {out.relative_to(ROOT)}"
              + (f"（{len(missing)} 則缺記者名）" if missing else ""))
        if idx and show:
            print(f"  索引：{idx.relative_to(ROOT)}")
        if show:
            print("─" * 40)
            print(text)

    report_summary(done, empty, show)


def report_summary(done, empty, show):
    for reason in dict.fromkeys(r for _, r in empty):
        names = [n for n, r in empty if r == reason]
        print(f"× 未產出（{reason}）：{'、'.join(names)}")
    if len(done) > 1 and not show:
        print(f"── 共 {len(done)} 家、{sum(d[1] for d in done)} 則。"
              "要看全文：./monitor build <客戶>")


if __name__ == "__main__":
    main()
