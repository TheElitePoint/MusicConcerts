#!/bin/bash
# Work the employment-verification backlog until it stops shrinking.
# The batch actor caps free accounts at 10 items per run, so a single invocation
# only ever clears 10 -- this repeats until there is no further progress.
cd /Users/anirudhtyagi/TEP/MusicIndustry
export PYTHONPATH=src
PREV=-1
for i in $(seq 1 12); do
  N=$(.venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from mp.db import Database
db=Database('data/master.sqlite')
print(db.one('''SELECT COUNT(*) n FROM people p
  JOIN companies c ON c.company_id=p.company_id
  JOIN profiles pr ON pr.person_id=p.person_id AND pr.channel='linkedin'
  WHERE c.status='QUALIFIED' AND p.current_verified=0 AND p.status!='REJECTED' ''')['n'])
db.close()")
  echo "--- pass $i: $N people still unverified ---"
  [ "$N" = "0" ] && break
  [ "$N" = "$PREV" ] && { echo "no progress; stopping"; break; }
  PREV=$N
  .venv/bin/python scripts/verify_profiles_batch.py 2>&1 | tail -3
  # The batch fetcher only WRITES evidence; current_verified is set by the verify
  # stage. Without running it here the backlog metric never moves and the loop
  # exits after one pass.
  .venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from mp.pipeline import Context
from mp.stages import s7_person_verify
ctx=Context.create(db_path='data/master.sqlite')
try: print('   verify ->', s7_person_verify.run(ctx))
finally: ctx.close()"
done
echo "=== verification passes complete ==="
.venv/bin/python scripts/rescore_and_export.py 2>&1 | tail -4
.venv/bin/python scripts/export_workbook.py 2>&1 | tail -9
