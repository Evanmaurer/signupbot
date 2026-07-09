#!/usr/bin/env bash
# Start the Discord bot (used by launchd/systemd or manual runs).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  echo "Virtualenv missing. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "Missing .env — copy .env.example to .env and set DISCORD_BOT_TOKEN."
  exit 1
fi

mkdir -p logs
exec .venv/bin/python bot.py
