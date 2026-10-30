#!/bin/bash
# Wait for any in-flight resolve, then verify -> gate -> backfill LinkedIn URLs.
cd /Users/anirudhtyagi/TEP/MusicIndustry
export PYTHONPATH=src
while pgrep -f "mp.cli resolve" >/dev/null 2>&1; do sleep 30; done
echo "--- resolve done, verifying ---"
.venv/bin/python -m mp.cli resolve --limit 250
.venv/bin/python -m mp.cli verify --limit 250
.venv/bin/python -m mp.cli gate
echo "--- backfilling linkedin urls for qualified ---"
.venv/bin/python scripts/backfill_company_linkedin.py
echo "=== CHAIN COMPLETE ==="
.venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from mp.db import Database
db=Database('data/master.sqlite')
for r in db.q('SELECT status, COUNT(*) n FROM companies GROUP BY status ORDER BY n DESC'):
    print(f'  {r[\"status\"]:12s} {r[\"n\"]}')
q=db.one(\"SELECT COUNT(*) n FROM companies WHERE status='QUALIFIED'\")['n']
l=db.one(\"SELECT COUNT(*) n FROM companies WHERE status='QUALIFIED' AND linkedin_url IS NOT NULL\")['n']
print(f'  qualified={q}  with LinkedIn URL={l}')
db.close()"
