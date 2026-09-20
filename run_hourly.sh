#!/bin/bash

cd /opt/btc-predict

PYTHON=/opt/btc-predict/.venv/bin/python

echo "===== $(date -u '+%Y-%m-%d %H:%M:%S UTC') ====="

echo "--- evaluate ---"
$PYTHON evaluate.py || echo "ERROR: evaluate failed"

echo "--- GPT prediction ---"
$PYTHON predict_gpt.py || echo "ERROR: GPT prediction failed"

echo "--- Jev prediction ---"
$PYTHON predict_jev.py || echo "ERROR: Jev prediction failed"

echo "--- done ---"
