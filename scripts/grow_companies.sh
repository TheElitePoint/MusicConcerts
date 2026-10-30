#!/bin/bash
# Free company growth: re-resolve unresolved, verify, and re-gate the REVIEW backlog.
# The people-search actors have exhausted the current qualified pool, so the only
# way to more rows is more QUALIFIED COMPANIES -- and that work costs nothing.
cd /Users/anirudhtyagi/TEP/MusicIndustry
export PYTHONPATH=src
echo "--- requeue unresolved (search has recovered since they failed) ---"
.venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from mp.db import Database
db=Database('data/master.sqlite')
print('requeued:', db.x(\"UPDATE companies SET status='NEW' WHERE status='UNRESOLVED'\").rowcount)
db.close()"
.venv/bin/python -m mp.cli resolve --limit 400
.venv/bin/python -m mp.cli verify  --limit 400
.venv/bin/python -m mp.cli gate
echo "=== GROWTH COMPLETE ==="
.venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from mp.db import Database
db=Database('data/master.sqlite')
one=lambda q: db.one(q)['n']
print('qualified:', one(\"SELECT COUNT(*) n FROM companies WHERE status='QUALIFIED'\"))
print('unworked :', one('''SELECT COUNT(*) n FROM companies c WHERE c.status='QUALIFIED'
   AND NOT EXISTS (SELECT 1 FROM people p WHERE p.company_id=c.company_id)'''))
db.close()"
