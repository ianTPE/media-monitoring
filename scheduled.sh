#!/usr/bin/env bash
# 排程用（crontab 呼叫）：只在台灣上班日執行（國定假日跳過、補班日照跑）。
#   ./scheduled.sh first   02:00 第一輪搜尋
#   ./scheduled.sh second  05:00 第二輪搜尋（只補新撈到的），跑完寄候選總覽信
#   ./scheduled.sh third   07:30 第三輪搜尋，跑完再寄一次（主旨標「更新版」）
# 紀錄寫在 .state/logs/<日期>-<輪次>.log
set -uo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

round="${1:?用法：./scheduled.sh first|second|third}"
today="$(TZ=Asia/Taipei date +%F)"
mkdir -p .state/logs
exec >>".state/logs/${today}-${round}.log" 2>&1
echo "==== $(date '+%F %T') ${round} ===="

if uv run --quiet --with pyyaml python -c '
import sys; sys.path.insert(0, "bin")
from datetime import date
from common import is_holiday
sys.exit(0 if is_holiday(date.fromisoformat(sys.argv[1])) else 1)' "$today"; then
  echo "今天不是上班日，跳過"
  exit 0
fi

# 兩輪都用 --merge：檔案不存在就新建，已存在就只補新的，不會清掉人工編輯。
if ./monitor fetch --merge; then
  ./monitor judge --date "$today" || echo "！Luna 判讀失敗（exit $?）；候選保留原規則"
else
  echo "！搜尋失敗（exit $?）"
fi

case "$round" in
  second) ./monitor email || echo "！寄信失敗（exit $?）" ;;
  # 05:00 若沒寄成（沒有寄送紀錄），這封就是當天第一封，不加「更新版」
  third)  if [ -e ".state/email-sent-${today}-all.json" ]; then
            ./monitor email --resend || echo "！寄信失敗（exit $?）"
          else
            ./monitor email || echo "！寄信失敗（exit $?）"
          fi ;;
esac
