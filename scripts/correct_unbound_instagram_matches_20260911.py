"""Demote same-name Instagram matches lacking target-employer evidence."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PERSON_IDS = (381, 2035, 363, 1700, 134, 569, 2348, 2457, 2445)


def main() -> int:
    now = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    try:
        for person_id in PERSON_IDS:
            con.execute(
                """UPDATE profiles SET verification_status='UNVERIFIED',identity_score=0,
                       usability='none',verified_at=NULL,retrieved_at=?,match_signals=?
                     WHERE person_id=? AND channel='instagram'""",
                (now, json.dumps({"rejected": "no_target_employer_binding"}), person_id),
            )
        con.commit()
    finally:
        con.close()
    print(json.dumps({"demoted_person_ids": PERSON_IDS, "reason": "no_target_employer_binding"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
