"""Demote Bob Becker's stale Fearless Records Instagram association."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/master.sqlite"


def main() -> int:
    now = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(DB, timeout=60)
    try:
        con.execute(
            """UPDATE profiles
                  SET verification_status='UNVERIFIED', identity_score=0,
                      usability='none', verified_at=NULL, retrieved_at=?,
                      match_signals=?
                WHERE person_id=705 AND channel='instagram'""",
            (now, json.dumps({
                "rejected": "conflicting_current_employer",
                "bio": "Founder Fearless Records, President @thrillerrecords",
            }, sort_keys=True)),
        )
        con.execute(
            """INSERT INTO person_evidence
               (person_id,kind,url,source,snippet,strength,retrieved_at)
               VALUES (705,'profile','https://www.instagram.com/fearbob/',
                       'apify_instagram_profile_conflict',?, 'strongest',?)
               ON CONFLICT(person_id,kind,url) DO UPDATE SET
                 source=excluded.source,snippet=excluded.snippet,
                 strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
            ("Rejected for Fearless Records: current Instagram bio identifies Bob Becker as President @thrillerrecords and links Thriller Records.", now),
        )
        con.commit()
    finally:
        con.close()
    print("person_id=705 instagram=UNVERIFIED reason=conflicting_current_employer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
