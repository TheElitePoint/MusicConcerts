#!/bin/bash
# Sustained push: run the two working name-based actors in parallel slices, then
# verify/score/export, and repeat. Stops when the LinkedIn sheet reaches TARGET or
# a round adds nothing (both actors exhausted).
cd /Users/anirudhtyagi/TEP/MusicIndustry
export PYTHONPATH=src
TARGET=${1:-100}
count_rows() {
  .venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from mp.db import Database
db=Database('data/master.sqlite')
print(db.one(\"SELECT COUNT(*) n FROM scores s JOIN people p USING(person_id) JOIN companies c ON c.company_id=p.company_id WHERE s.channel='linkedin' AND s.verdict='FINAL PASS' AND p.status!='REJECTED' AND c.status='QUALIFIED'\")['n'])
db.close()"
}
PREV=$(count_rows)
echo "starting at $PREV rows, target $TARGET"
for round in 1 2 3 4 5 6; do
  echo "===== ROUND $round ====="
  # Two actors x two slices = four concurrent workers on separate allowances.
  for spec in "candidate 0 2" "candidate 1 2" "people 0 2" "people 1 2"; do
    set -- $spec
    .venv/bin/python scripts/parallel_people.py "$1" "$2" "$3" \
      > "/tmp/mp_push_${1}_${2}.log" 2>&1 &
  done
  wait
  for f in /tmp/mp_push_*.log; do tail -2 "$f" | head -1; done
  .venv/bin/python scripts/rescore_and_export.py 2>&1 | tail -2
  NOW=$(count_rows)
  echo "  rows: $PREV -> $NOW"
  .venv/bin/python scripts/export_workbook.py > /dev/null 2>&1
  .venv/bin/python scripts/export_new_data.py 2>&1 | head -2
  if [ "$NOW" -ge "$TARGET" ]; then echo "TARGET REACHED: $NOW rows"; break; fi
  if [ "$NOW" -le "$PREV" ]; then echo "no gain this round; actors likely exhausted"; break; fi
  PREV=$NOW
done
echo "===== PUSH COMPLETE ====="
