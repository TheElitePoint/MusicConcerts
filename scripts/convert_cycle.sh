#!/bin/bash
# Verification -> rescore -> export, repeated until it stops adding rows.
cd /Users/anirudhtyagi/TEP/MusicIndustry
export PYTHONPATH=src
for cycle in 1 2 3; do
  echo "===== CYCLE $cycle ====="
  .venv/bin/python scripts/verify_profiles_batch.py
  .venv/bin/python scripts/rescore_and_export.py 2>&1 | tail -4
  .venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from mp.db import Database
db=Database('data/master.sqlite')
li=db.one(\"SELECT COUNT(*) n FROM scores WHERE channel='linkedin' AND verdict='FINAL PASS'\")['n']
ig=db.one(\"SELECT COUNT(*) n FROM scores WHERE channel='instagram' AND verdict='FINAL PASS'\")['n']
print(f'  rows now: linkedin={li} instagram={ig} total={li+ig}')
db.close()"
done
echo "===== CYCLES COMPLETE ====="
