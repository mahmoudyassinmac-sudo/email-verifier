#!/bin/bash
# One-time setup on macOS: virtual env + dependencies + .env
set -e
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
[ -f .env ] || cp .env.example .env
chmod +x scripts/*.sh
echo ""
echo "Setup complete."
echo "  Dashboard : ./scripts/start_dashboard.sh   -> http://127.0.0.1:8765"
echo "  Terminal  : source .venv/bin/activate && python -m verifier run ~/path/to/list.csv"
