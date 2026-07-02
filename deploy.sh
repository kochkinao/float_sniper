#!/usr/bin/env bash
set -euo pipefail

cd /home/hermes/cs2-float-sniper

echo "== git pull =="
git pull

echo "== syntax check =="
python3 -m py_compile steam_alert_bot.py

echo "== restart pm2 =="
pm2 restart cs2-float-sniper

echo "== status =="
pm2 status cs2-float-sniper

echo "== recent logs =="
pm2 logs cs2-float-sniper --lines 80 --nostream
