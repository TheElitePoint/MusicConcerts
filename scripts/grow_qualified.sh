#!/bin/bash
# Push RESOLVED -> VERIFIED -> QUALIFIED, and re-resolve UNRESOLVED now that
# Tavily search is working (it was throttled when they first failed).
cd /Users/anirudhtyagi/TEP/MusicIndustry
export PYTHONPATH=src
echo "--- verify the 41 already-resolved ---"
.venv/bin/python -m mp.cli verify --limit 60
echo "--- retry unresolved with Tavily live ---"
.venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from mp.db import Database
db=Database('data/master.sqlite')
n=db.x(\"UPDATE companies SET status='NEW' WHERE status='UNRESOLVED'\").rowcount
print('requeued', n, 'unresolved companies')
db.close()"
.venv/bin/python -m mp.cli resolve --limit 140
.venv/bin/python -m mp.cli verify --limit 140
.venv/bin/python -m mp.cli gate
echo "=== GROW COMPLETE ==="
.venv/bin/python scripts/inventory.py 2>/dev/null | head -20
