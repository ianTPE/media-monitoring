"""用 Prefect 排程每日新聞監測：每一輪拆成「上班日檢查 → 搜尋 → Luna 判讀 → 寄信」。

網頁介面 http://localhost:4200 可看每一輪每一步的狀態、耗時與輸出，也可手動重跑。
每一步直接呼叫既有的 ./monitor 指令，流程內容與 scheduled.sh 相同。

環境變數（寫在 systemd 服務檔）：
  MM_SEND_EMAIL=1        真的寄信；未設＝只產生寄信預覽（與 crontab 並行測試時用）
  MM_TIMES="0 2,0 5,30 7"  三輪的「分 時」；並行測試時錯開為 "20 2,20 5,50 7"
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from prefect import flow, get_run_logger, task
from prefect.schedules import Cron

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
from common import TPE, is_holiday  # noqa: E402

ENV = {**os.environ,
       "PATH": f"{ROOT}/.state/codex-cli/node_modules/.bin:{Path.home()}/.local/bin:"
               "/usr/local/bin:/usr/bin:/bin"}


def monitor(*args):
    """執行 ./monitor 指令，輸出逐行寫進 Prefect 紀錄；失敗就丟出例外讓這一步標成失敗。"""
    log = get_run_logger()
    log.info("執行：./monitor %s", " ".join(args))
    proc = subprocess.Popen(["./monitor", *args], cwd=ROOT, env=ENV, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for line in proc.stdout:
        if line.strip():
            log.info(line.rstrip())
    if proc.wait():
        raise RuntimeError(f"./monitor {' '.join(args)} 失敗（exit {proc.returncode}）")


@task(name="上班日檢查")
def workday(day):
    return not is_holiday(day)


@task(name="搜尋新聞", retries=2, retry_delay_seconds=300)
def fetch():
    monitor("fetch", "--merge")


@task(name="Luna 判讀", retries=1, retry_delay_seconds=120)
def judge(day):
    monitor("judge", "--date", str(day))


@task(name="寄候選總覽信", retries=2, retry_delay_seconds=120)
def email(day, resend, send):
    args = ["email"] + (["--resend"] if resend else []) + ([] if send else ["--dry-run"])
    monitor(*args)


@flow(name="每日新聞監測", log_prints=True)
def daily(round: str, send_email: bool = False):
    log = get_run_logger()
    day = datetime.now(TPE).date()
    if not workday(day):
        log.info("%s 不是上班日，跳過", day)
        return "假日跳過"
    fetch()
    # 判讀失敗不影響寄信：候選照原規則保留（與 scheduled.sh 相同）
    state = judge(day, return_state=True)
    if state.is_failed():
        log.warning("Luna 判讀失敗，候選保留原規則")
    if round in ("second", "third"):
        sent_before = (ROOT / ".state" / f"email-sent-{day}-all.json").exists()
        email(day, resend=(round == "third" and sent_before), send=send_email)
    return "完成"


if __name__ == "__main__":
    send = os.environ.get("MM_SEND_EMAIL") == "1"
    times = (os.environ.get("MM_TIMES") or "0 2,0 5,30 7").split(",")
    rounds = ["first", "second", "third"]
    daily.serve(
        name="排程",
        schedules=[Cron(f"{t.strip()} * * *", timezone="Asia/Taipei", slug=r,
                        parameters={"round": r, "send_email": send})
                   for r, t in zip(rounds, times)])
