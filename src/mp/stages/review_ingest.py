"""Re-ingest human verdicts from a review CSV.

The brief requires that adjudicated items never resurface. A verdict written here
is stored in `review_verdicts` and applied to the entity's status permanently, so
subsequent runs skip the company before making any network call.

Only the `verdict` column is read. `suggested_verdict` (Groq) is advisory and is
deliberately ignored unless a human copied it into `verdict`.
"""
from __future__ import annotations

import csv
from pathlib import Path

from ..db import utcnow
from ..pipeline import Context

VALID = {"PASS", "REJECT", "RESEARCH_FURTHER"}

# A human REJECT is permanent; a human PASS re-opens the record for person research.
STATUS_FOR = {"PASS": "QUALIFIED", "REJECT": "REJECTED", "RESEARCH_FURTHER": "REVIEW"}


def run(ctx: Context, csv_path: str | Path) -> dict:
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"review file not found: {path}")

    applied = skipped = invalid = 0

    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            verdict = (row.get("verdict") or "").strip().upper().replace(" ", "_")
            if not verdict:
                skipped += 1
                continue
            if verdict not in VALID:
                invalid += 1
                continue

            entity_type = (row.get("entity_type") or "company").strip()
            try:
                entity_id = int(row.get("entity_id") or 0)
            except ValueError:
                invalid += 1
                continue
            if not entity_id:
                invalid += 1
                continue

            ctx.db.x("""INSERT OR REPLACE INTO review_verdicts
                        (entity_type, entity_id, verdict, reason, decided_by, decided_at)
                        VALUES (?,?,?,?,?,?)""",
                     (entity_type, entity_id, verdict,
                      (row.get("suggested_reason") or "")[:400], "human", utcnow()))

            if entity_type == "company":
                ctx.db.x("UPDATE companies SET status=?, last_researched=? WHERE company_id=?",
                         (STATUS_FOR[verdict], utcnow(), entity_id))
                if verdict == "REJECT":
                    # recheck_after NULL -> permanent; the skip index will never
                    # surface this company again.
                    ctx.db.x("""INSERT INTO company_rejections
                                (company_id, gate, reason_code, reason_text,
                                 rejected_at, recheck_after)
                                VALUES (?,?,?,?,?,NULL)""",
                             (entity_id, "HUMAN", "human_review",
                              "Rejected by human review", utcnow()))
            else:
                ctx.db.x("UPDATE people SET status=?, last_researched=? WHERE person_id=?",
                         ("VERIFIED" if verdict == "PASS" else "REJECTED",
                          utcnow(), entity_id))
            applied += 1

    stats = {"applied": applied, "skipped_blank": skipped, "invalid": invalid}
    ctx.stats.setdefault("review_ingest", {}).update(stats)
    return stats
