# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""08:00 保險檢查（crontab 呼叫）：上班日到這時還沒寄出候選總覽信，就補跑一輪並通知。

不管平常是 crontab 或 Prefect 在跑，只看「今天有沒有寄送紀錄」，
所以排程默默沒跑（例如 Prefect 排程服務壞掉）也抓得到。
通知寄給 .env 的 ALERT_TO；沒設就寄給 MAIL_TO 的第一個地址。
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, TPE, is_holiday
from email_digest import read_env, send


def tail(path, n=15):
    if not path.exists():
        return "（沒有紀錄檔）"
    return "\n".join(path.read_text(encoding="utf-8").splitlines()[-n:])


def main():
    now = datetime.now(TPE)
    day = now.date()
    if is_holiday(day):
        print(f"{day} 不是上班日，跳過")
        return
    receipt = ROOT / ".state" / f"email-sent-{day}-all.json"
    if receipt.exists():
        print(f"{day} 已寄出候選總覽信，正常")
        return

    print(f"！{day} {now:%H:%M} 還沒寄出候選總覽信，用 scheduled.sh 補跑一輪")
    logs = ROOT / ".state" / "logs"
    try:
        subprocess.run([str(ROOT / "scheduled.sh"), "fallback"], cwd=ROOT, timeout=2400)
        ok = receipt.exists()
    except subprocess.TimeoutExpired:
        ok = False
    result = ("補跑成功，候選總覽信已寄出。" if ok
              else "補跑也沒寄成，今天的候選總覽信需要人工處理。")
    print(result)

    body = "\n".join([
        f"{day} 到 {now:%H:%M} 都沒有寄出候選總覽信（找不到 {receipt.name}）。",
        f"已用舊方法（scheduled.sh）補跑一輪：{result}",
        "",
        "可能原因：排程沒有觸發、搜尋或寄信失敗。",
        "Prefect 介面：http://localhost:4200",
        "紀錄檔：.state/logs/",
        "",
        *[f"── {p.name}（最後 15 行）──\n{tail(p)}\n"
          for p in sorted(logs.glob(f"{day}-*.log"))],
    ])
    settings = read_env(ROOT / ".env")
    alert_to = settings.get("ALERT_TO") or settings["MAIL_TO"].split(",")[0].strip()
    subject = f"【新聞監測警告】{day} 08:00 尚未寄出候選總覽信" + ("（已補寄）" if ok else "（補跑失敗）")
    send(subject, body, {**settings, "MAIL_TO": alert_to})
    print(f"已通知 {alert_to}")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
