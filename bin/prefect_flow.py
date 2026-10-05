"""用 Prefect 排程每日新聞監測（2026-10-05 起取代 crontab 的三輪，由這裡真的寄信）。

每一輪：上班日檢查 → 前置檢查（三項同時跑）→ 搜尋 → Luna 判讀 → 寄信 → 本輪摘要。
網頁介面 http://localhost:4200 可看每一輪每一步的狀態、耗時與輸出，也可手動重跑。
每一步直接呼叫既有的 ./monitor 指令，流程內容與 scheduled.sh 相同；輸出也照舊
寫進 .state/logs/<日期>-<輪次>.log，08:00 保險檢查（crontab）寄警告信時會附上。

用來學 Prefect 的地方（程式裡標「學習點」）：
  1. flow／task：@flow 是一輪，@task 是一步；介面的 Runs 點進去看到的方塊就是 task
  2. 重試：task 的 retries；失敗會先變 AwaitingRetry，過幾分鐘自己再跑
  3. 平行：前置檢查用 .submit() 同時送出三個 task，介面時間軸上會疊在一起
  4. 狀態：return_state=True 拿到 State 自己判斷，失敗不讓整輪中止
  5. Artifacts：每輪留一張「各客戶則數」表和一份摘要，Artifacts 頁可按日期翻歷史
  6. Events：寄信成功／失敗、搜尋失敗都發事件，Events 頁看得到；之後可用
     Automations 設「收到某事件就做某事」（例如寄信失敗就通知）
  7. 自訂執行名稱：每輪叫「10/05 第二輪」而不是隨機的 loud-corgi

環境變數（寫在 systemd 服務檔）：
  MM_SEND_EMAIL=1        真的寄信；未設＝只產生寄信預覽
  MM_TIMES="0 2,0 5,30 7"  三輪的「分 時」
"""

import os
import re
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from prefect import flow, get_run_logger, task
from prefect.artifacts import create_markdown_artifact, create_table_artifact
from prefect.events import emit_event
from prefect.runtime import flow_run
from prefect.schedules import Cron

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
from common import TPE, is_holiday  # noqa: E402

ENV = {**os.environ,
       "PATH": f"{ROOT}/.state/codex-cli/node_modules/.bin:{Path.home()}/.local/bin:"
               "/usr/local/bin:/usr/bin:/bin"}
ROUND_NAMES = {"first": "第一輪", "second": "第二輪", "third": "第三輪"}


def log_file():
    """與 scheduled.sh 同一個紀錄檔：.state/logs/<日期>-<輪次>.log。"""
    day = datetime.now(TPE).date()
    path = ROOT / ".state" / "logs" / f"{day}-{flow_run.parameters.get('round', 'manual')}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def monitor(*args):
    """執行 ./monitor 指令，輸出逐行寫進 Prefect 紀錄與 .state/logs；回傳輸出各行。
    失敗就丟出例外讓這一步標成失敗（有設 retries 的會自動重試）。"""
    log = get_run_logger()
    log.info("執行：./monitor %s", " ".join(args))
    lines = []
    with open(log_file(), "a", encoding="utf-8") as out:
        out.write(f"==== {datetime.now(TPE):%F %T} ./monitor {' '.join(args)}（Prefect）====\n")
        proc = subprocess.Popen(["./monitor", *args], cwd=ROOT, env=ENV, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for line in proc.stdout:
            out.write(line)
            if line.strip():
                log.info(line.rstrip())
                lines.append(line.rstrip())
        if proc.wait():
            out.write(f"！./monitor {' '.join(args)} 失敗（exit {proc.returncode}）\n")
            raise RuntimeError(f"./monitor {' '.join(args)} 失敗（exit {proc.returncode}）")
    return lines


def run_name():
    """學習點 7：執行名稱可以用函式算，參數從 prefect.runtime 拿。"""
    r = flow_run.parameters.get("round", "")
    return f"{datetime.now(TPE):%m/%d} {ROUND_NAMES.get(r, r)}"


@task(name="上班日檢查")
def workday(day):
    return not is_holiday(day)


# ── 前置檢查：只提醒、不擋流程。三項互不相干，所以用 .submit() 同時跑（學習點 3）──

@task(name="檢查 Luna 登入", retries=1, retry_delay_seconds=30)
def check_luna():
    from subscription_judge import subscription_codex
    subscription_codex()          # 不是 ChatGPT 訂閱登入會丟出例外
    return "Luna（codex）已用 ChatGPT 訂閱登入"


@task(name="檢查 Langfuse")
def check_langfuse():
    from email_digest import read_env
    host = read_env(ROOT / ".env").get("LANGFUSE_HOST")
    if not host:
        return "沒設 LANGFUSE_HOST，不記錄軌跡"
    with urllib.request.urlopen(host.rstrip("/") + "/api/public/health", timeout=10) as r:
        return f"Langfuse 正常（HTTP {r.status}）"


@task(name="檢查寄信伺服器")
def check_smtp():
    from email_digest import read_env
    env = read_env(ROOT / ".env")
    # 只確認連得上，不登入、不寄信
    with socket.create_connection((env["SMTP_HOST"], int(env["SMTP_PORT"])), timeout=10):
        return f"連得上寄信伺服器 {env['SMTP_HOST']}:{env['SMTP_PORT']}"


# ── 主要步驟 ──

@task(name="搜尋新聞", retries=2, retry_delay_seconds=300)   # 學習點 2
def fetch():
    return monitor("fetch", "--merge")


@task(name="Luna 判讀", retries=1, retry_delay_seconds=120)
def judge(day):
    return monitor("judge", "--date", str(day))


@task(name="寄候選總覽信", retries=2, retry_delay_seconds=120)
def email(day, resend, send):
    args = ["email"] + (["--resend"] if resend else []) + ([] if send else ["--dry-run"])
    return monitor(*args)


@task(name="本輪摘要")
def summarize(day, round, fetch_lines, email_lines, notes):
    """學習點 5：Artifacts。key 固定，同一個 key 每輪留一版，Artifacts 頁可看歷史。"""
    rows = []
    for line in fetch_lines:
        m = re.search(r"[＋✚+]\s*(\S+)：補進 (\d+) 則，共 (\d+) 則（勾選 (\d+)）", line)
        if m:
            rows.append({"客戶": m.group(1), "本輪補進": int(m.group(2)),
                         "候選共": int(m.group(3)), "預設勾選": int(m.group(4))})
    if rows:
        create_table_artifact(key="client-counts", table=rows,
                              description=f"{day} {ROUND_NAMES.get(round, round)}：各客戶候選則數")
    picked = lambda pat, lines: next((m.group(0) for l in lines for m in [re.search(pat, l)] if m), "")
    md = "\n".join([
        f"# {day} {ROUND_NAMES.get(round, round)}",
        "",
        "## 搜尋",
        f"- {picked(r'Google News 回傳.*', fetch_lines) or '（沒有搜尋輸出）'}",
        f"- {picked(r'網址還原完成.*', fetch_lines) or '（沒有網址還原紀錄）'}",
        f"- {picked(r'轉載：.*', fetch_lines) or '（沒有轉載處理紀錄）'}",
        "",
        *(["## 寄信",
           f"- {picked(r'預覽：.*', email_lines) or '（沒有寄信輸出）'}",
           f"- {picked(r'已寄至.*', email_lines) or '沒有真的寄出（預覽模式或失敗）'}",
           ""] if round != "first" else []),
        "## 提醒",
        *([f"- {n}" for n in notes] or ["- 無"]),
    ])
    create_markdown_artifact(key="round-summary", markdown=md,
                             description=f"{day} {ROUND_NAMES.get(round, round)}摘要")


def event(name, day, round, **payload):
    """學習點 6：發事件。resource id 是「這件事跟誰有關」，Automations 用它來篩選。"""
    emit_event(event=f"media-monitoring.{name}",
               resource={"prefect.resource.id": f"media-monitoring.digest.{day}",
                         "prefect.resource.name": f"{day} 候選總覽"},
               payload={"round": round, **payload})


@flow(name="每日新聞監測", flow_run_name=run_name, log_prints=True)
def daily(round: str, send_email: bool = False):
    log = get_run_logger()
    day = datetime.now(TPE).date()
    if not workday(day):
        log.info("%s 不是上班日，跳過", day)
        return "假日跳過"
    notes = []

    # 學習點 3＋4：同時送出，再逐一拿 State；失敗只記提醒
    checks = [check_luna.submit(), check_langfuse.submit(), check_smtp.submit()]
    for future in checks:
        future.wait()
        state = future.state
        if state.is_completed():
            log.info("前置檢查：%s", future.result())
        else:
            notes.append(f"前置檢查沒過：{state.message or state.name}")
            log.warning(notes[-1])

    # 搜尋失敗（重試完還是失敗）照 scheduled.sh：不判讀，但仍用上一輪的候選寄信
    fetch_lines = []
    state = fetch(return_state=True)
    if state.is_completed():
        fetch_lines = state.result()
        judged = judge(day, return_state=True)
        if not judged.is_completed():
            notes.append("Luna 判讀失敗，候選保留原規則")
            log.warning(notes[-1])
    else:
        notes.append("搜尋失敗，沿用上一輪的候選")
        log.warning(notes[-1])
        event("fetch.failed", day, round)

    email_lines = []
    if round in ("second", "third"):
        sent_before = (ROOT / ".state" / f"email-sent-{day}-all.json").exists()
        resend = round == "third" and sent_before
        state = email(day, resend=resend, send=send_email, return_state=True)
        if state.is_completed():
            email_lines = state.result()
            if send_email:
                event("email.sent", day, round, resend=resend)
        else:
            notes.append("寄信失敗；08:00 保險檢查會補寄")
            event("email.failed", day, round)

    summarize(day, round, fetch_lines, email_lines, notes)
    if round in ("second", "third") and not email_lines:
        raise RuntimeError("寄信失敗")      # 讓這一輪在介面上標紅
    return "完成" if not notes else "完成（有提醒）"


if __name__ == "__main__":
    send = os.environ.get("MM_SEND_EMAIL") == "1"
    times = (os.environ.get("MM_TIMES") or "0 2,0 5,30 7").split(",")
    rounds = ["first", "second", "third"]
    daily.serve(
        name="排程",
        tags=["新聞監測"],
        schedules=[Cron(f"{t.strip()} * * *", timezone="Asia/Taipei", slug=r,
                        parameters={"round": r, "send_email": send})
                   for r, t in zip(rounds, times)])
