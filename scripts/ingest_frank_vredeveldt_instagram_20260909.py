"""Ingest the signed-in Instagram verification for Frank Vredeveldt."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/master.sqlite"
PERSON_ID = 2836
HANDLE = "frankvredeveldt"


def main() -> int:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    url = f"https://www.instagram.com/{HANDLE}/"
    signals = {
        "matches_linkedin_identity": True,
        "company_in_bio": True,
        "role_in_bio": True,
        "username_resembles_name": False,
    }
    con = sqlite3.connect(DB, timeout=60)
    try:
        row = con.execute(
            """SELECT p.full_name,p.title,p.current_verified,p.status,c.canonical_name,c.status
                 FROM people p JOIN companies c USING(company_id)
                WHERE p.person_id=?""",
            (PERSON_ID,),
        ).fetchone()
        if not row or row[2] != 1 or row[3] == "REJECTED" or row[5] != "QUALIFIED":
            raise RuntimeError("person or company no longer meets the verified-current gates")
        con.execute(
            """INSERT INTO profiles
               (person_id,channel,url,handle,verification_status,identity_score,
                usability,match_signals,verified_at,retrieved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(person_id,channel) DO UPDATE SET
                 url=excluded.url,handle=excluded.handle,
                 verification_status=excluded.verification_status,
                 identity_score=excluded.identity_score,usability=excluded.usability,
                 match_signals=excluded.match_signals,verified_at=excluded.verified_at,
                 retrieved_at=excluded.retrieved_at""",
            (PERSON_ID, "instagram", url, HANDLE, "VERIFIED", 8, "partial",
             json.dumps(signals, sort_keys=True), timestamp, timestamp),
        )
        con.execute(
            """INSERT INTO person_evidence
               (person_id,kind,url,source,snippet,strength,retrieved_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(person_id,kind,url) DO UPDATE SET
                 source=excluded.source,snippet=excluded.snippet,
                 strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
            (PERSON_ID, "profile", url, "instagram_signed_in_profile",
             "Signed-in direct profile check: exact display name Frank Vredeveldt; bio states Head of A&R Soave Records; private but messageable profile.",
             "strongest", timestamp),
        )
        con.commit()
    finally:
        con.close()
    print(json.dumps({"person_id": PERSON_ID, "instagram_url": url, "identity_score": 8}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
