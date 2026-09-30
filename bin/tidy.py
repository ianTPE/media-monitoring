# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""把既有檔案搬到目前 defaults.yaml 設定的資料夾結構；也可清掉太舊的候選檔。"""
import argparse, re, sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, TPE, load_clients, out_path

DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")


def files_of(kind, cfg):
    for f in (ROOT / kind).rglob("*.md"):
        m = DATE.match(f.stem)
        if m and cfg["id"] in f.stem:
            yield datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=TPE), f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("client", nargs="?")
    ap.add_argument("--prune-candidates", type=int, metavar="N",
                    help="列出 N 天前的候選檔（報告不動）")
    ap.add_argument("--yes", action="store_true", help="確認刪除 --prune-candidates 列出的檔案")
    args = ap.parse_args()

    moved = stale = 0
    for cfg in load_clients(args.client):
        for kind in ("candidates", "reports"):
            for day, f in sorted(files_of(kind, cfg)):
                target = out_path(kind, cfg, day)
                if f.resolve() == target.resolve():
                    continue
                if target.exists():
                    print(f"  ! 目標已存在，跳過：{target.relative_to(ROOT)}")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                f.rename(target)
                print(f"  → {f.relative_to(ROOT)}  ⇒  {target.relative_to(ROOT)}")
                moved += 1

        if args.prune_candidates:
            cutoff = datetime.now(TPE) - timedelta(days=args.prune_candidates)
            old = [f for day, f in sorted(files_of("candidates", cfg)) if day < cutoff]
            for f in old:
                print(("  ✗ 已刪除 " if args.yes else "  · 可刪除 ") + str(f.relative_to(ROOT)))
                if args.yes:
                    f.unlink()
            stale += len(old)

    # 清掉搬空的資料夾
    for d in sorted((ROOT / "candidates").rglob("*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()

    print(f"✔ 搬移 {moved} 個檔案" + (
        f"；{stale} 個舊候選檔{'已刪除' if args.yes else '待確認（加 --yes 才會真的刪）'}"
        if args.prune_candidates else ""))


if __name__ == "__main__":
    main()
