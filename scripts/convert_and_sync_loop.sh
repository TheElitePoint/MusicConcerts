#!/bin/bash
# While the main loop sweeps companies (S5), convert people already found into
# final rows -- profiles -> verify -> batch verify -> rescore -> sheet sync -- every pass.
cd /Users/anirudhtyagi/TEP/MusicIndustry
export PYTHONPATH=src
pass=0
while [ -f data/loop.pid ] && kill -0 "$(cat data/loop.pid)" 2>/dev/null; do
  pass=$((pass+1))
  echo "===== PASS $pass $(date -u +%m-%dT%H:%M:%S) ====="
  .venv/bin/python scripts/resolve_profiles_and_export.py 2>&1 | grep -E "^(profiles|verify|score|export)" | cut -c1-300
  .venv/bin/python scripts/rescore_and_export.py 2>&1 | tail -3 | cut -c1-300
  .venv/bin/python scripts/sync_linkedin_cycle_to_sheet.py 2>&1 | tail -1 | cut -c1-400
  .venv/bin/python scripts/update_apify_key_sheet.py 2>&1 | tail -1 | cut -c1-200
  sleep 1200
done
