#!/bin/bash
set -eu
cd "$(dirname "$0")"
PYTHON="${BTC_PREDICT_PYTHON:-$PWD/.venv/bin/python}"
echo "===== $(date -u '+%Y-%m-%d %H:%M:%S UTC') ====="
"$PYTHON" evaluate.py
"$PYTHON" next_stages.py
