#!/bin/bash
# Run the LinkedIn sheet sync once after every completed loop cycle (STATE line).
cd /Users/anirudhtyagi/TEP/MusicIndustry
seen=$(grep -c 'STATE' data/logs/mp_loop.log 2>/dev/null | head -1)
while [ -f data/loop.pid ] && kill -0 "$(cat data/loop.pid)" 2>/dev/null; do
  now=$(grep -c 'STATE' data/logs/mp_loop.log 2>/dev/null | head -1)
  if [ "$now" -gt "$seen" ]; then
    seen=$now
    echo "[$(date -u +%H:%M:%S)] cycle $now done"
    PYTHONPATH=src .venv/bin/python scripts/sync_linkedin_cycle_to_sheet.py 2>&1 | tail -3
  fi
  sleep 30
done
