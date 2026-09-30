#!/bin/bash
set -eu
cd "$(dirname "$0")"
PYTHON="${BTC_PREDICT_PYTHON:-$PWD/.venv/bin/python}"
echo "===== $(date -u '+%Y-%m-%d %H:%M:%S UTC') ====="
"$PYTHON" evaluate.py
status=0
"$PYTHON" predict_gpt.py || status=1
"$PYTHON" predict_jev.py || status=1
exit "$status"
