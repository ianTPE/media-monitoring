# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Email one combined, human-reviewable digest of all client candidates."""

import argparse
import json
import re
import smtplib
import ssl
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, TPE, find_existing, load_clients
from judgment_rules import keep_company_title

ROW = re.compile(r"^- \[([ xX])\]\s*(.+)$")
URL = re.compile(r"^\s*(https?://\S+)")
ADDRESS = re.compile(r"^[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+$")


def read_env(path):
    if not path.is_file():
        raise ValueError(f"找不到 {path.name}；請先依 .env.example 建立")
    if path.stat().st_mode & 0o077:
        raise ValueError(f"{path.name} 權限過寬；請先執行 chmod 600 {path.name}")
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            raise ValueError(f"{path.name} 有無效設定行")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value.startswith(("'", '"')) and value.endswith(value[:1]):
            value = value[1:-1]
        values[key] = value
    return values


def parse_candidates(path):
    items, current = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        row = ROW.match(line)
        if row:
            parts = [p.strip() for p in row.group(2).split("｜", 2)]
            parts += [""] * (3 - len(parts))
            current = {"checked": row.group(1).lower() == "x",
                       "source": parts[0], "reporter": parts[1],
                       "title": parts[2], "url": "", "ai_rejected": False}
            items.append(current)
        elif current is not None:
            url = URL.match(line)
            if url:
                current["url"] = url.group(1)
                current["ai_rejected"] = ("AI判讀：非新聞" in line
                                          or "AI判讀：不相關" in line)
                current = None
    return [item for item in items if item["title"]]


def compile_digest(day, checked_only=False):
    sections, total, selected = [], 0, 0
    for cfg in load_clients():
        path = find_existing("candidates", cfg, day)
        if not path.is_file():
            raise ValueError(f"缺少 {cfg['name']} 的候選檔：{path.relative_to(ROOT)}")
        items = [item for item in parse_candidates(path)
                 if (not item["ai_rejected"] or item["checked"]
                     or keep_company_title(cfg, item["title"], item["url"]))]
        total += len(items)
        selected += sum(item["checked"] for item in items)
        sections.append((cfg, [item for item in items
                               if item["checked"] or not checked_only]))

    shown = selected if checked_only else total
    lines = [f"{day:%Y-%m-%d} 每日新聞監測候選總覽", "",
             f"20 家客戶｜{shown} 則新聞" + ("（僅含原先勾選項目）" if checked_only else ""),
             "此為搜尋候選，尚未經人工查核及排序。", ""]
    for index, (cfg, items) in enumerate(sections):
        if index:
            lines += ["---", ""]
        lines += [f"{cfg['name']}【 {day.month}/{day.day} 新聞監測】", ""]
        if not items:
            lines += ["（無候選新聞）", ""]
            continue
        for number, item in enumerate(items, 1):
            prefix = f"{number}." if cfg.get("numbered", True) else ""
            who = f" {item['reporter']}" if item["reporter"] else ""
            lines += [f"{prefix}{item['source']}{who}",
                      item["title"], item["url"] or "（缺少連結）", ""]
    subject = f"每日新聞監測候選｜{day:%Y-%m-%d}｜20 家｜{shown} 則"
    return subject, "\n".join(lines).rstrip() + "\n", total, selected


def send(subject, body, settings):
    required = ("SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY",
                "SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_TO")
    missing = [key for key in required if not settings.get(key)]
    if missing:
        raise ValueError(f".env 尚缺：{', '.join(missing)}")
    sender = settings.get("MAIL_FROM") or settings["SMTP_USERNAME"]
    # MAIL_TO 可填多個地址，用逗號分隔
    recipients = [a.strip() for a in settings["MAIL_TO"].split(",") if a.strip()]
    bad = [a for a in [sender] + recipients if not ADDRESS.fullmatch(a)]
    if not recipients or bad:
        raise ValueError(f"MAIL_FROM 或 MAIL_TO 有不是有效電子郵件的地址：{', '.join(bad)}")
    recipient = ", ".join(recipients)
    port = int(settings["SMTP_PORT"])
    security = settings["SMTP_SECURITY"].lower()
    if security not in ("ssl", "starttls"):
        raise ValueError("SMTP_SECURITY 只能是 ssl 或 starttls")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(TPE))
    msg["Message-ID"] = make_msgid(domain=sender.split("@", 1)[1])
    msg.set_content(body, charset="utf-8")
    context = ssl.create_default_context()
    if security == "ssl":
        with smtplib.SMTP_SSL(settings["SMTP_HOST"], port, timeout=30,
                              context=context) as smtp:
            smtp.login(settings["SMTP_USERNAME"], settings["SMTP_PASSWORD"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(settings["SMTP_HOST"], port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(settings["SMTP_USERNAME"], settings["SMTP_PASSWORD"])
            smtp.send_message(msg)
    return msg["Message-ID"], recipient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="候選日期 YYYY-MM-DD；預設今天（台灣時間）")
    ap.add_argument("--checked-only", action="store_true", help="只寄預設勾選的候選")
    ap.add_argument("--dry-run", action="store_true", help="產生預覽，但不連線或寄信")
    ap.add_argument("--resend", action="store_true", help="允許重寄同日期、同模式的摘要")
    args = ap.parse_args()
    day = (datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=TPE)
           if args.date else datetime.now(TPE))
    try:
        subject, body, total, selected = compile_digest(day, args.checked_only)
        if args.resend:
            subject += "（更新版）"
        mode = "checked" if args.checked_only else "all"
        preview = ROOT / ".state" / f"email-preview-{day:%Y-%m-%d}-{mode}.txt"
        preview.parent.mkdir(exist_ok=True)
        preview.write_text(f"主旨：{subject}\n\n{body}", encoding="utf-8")
        print(f"預覽：{preview.relative_to(ROOT)}（候選 {total} 則，預設勾選 {selected} 則）")
        if args.dry_run:
            return
        receipt = ROOT / ".state" / f"email-sent-{day:%Y-%m-%d}-{mode}.json"
        if receipt.exists() and not args.resend:
            raise ValueError(f"此日期與模式已有寄送紀錄：{receipt.name}；確定重寄才加 --resend")
        settings = read_env(ROOT / ".env")
        message_id, recipient = send(subject, body, settings)
        history = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else []
        if isinstance(history, dict):
            history = [history]
        history.append({"message_id": message_id, "to": recipient,
                        "sent_at": datetime.now(TPE).isoformat(),
                        "count": selected if args.checked_only else total})
        receipt.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
        print(f"已寄至 {recipient}（{message_id}）")
    except (ValueError, OSError, smtplib.SMTPException) as exc:
        raise SystemExit(f"未寄送：{exc.__class__.__name__}: {exc}") from None


if __name__ == "__main__":
    main()
