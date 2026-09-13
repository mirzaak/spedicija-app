#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/backend"
unset PYTHONPATH
unset PYTHONHOME
echo "http://localhost:8000"
PYTHONUNBUFFERED=1 "$SCRIPT_DIR/venv.nosync/bin/python3.13" -m uvicorn main:app --port 8000
