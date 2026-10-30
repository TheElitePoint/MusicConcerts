"""Ingest the SOP-valid profiles from the no-company-Instagram expansion."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUDITS = [
    ROOT / "data/instagram_runs/direct_profiles_no_company_ig_20260902.json",
    ROOT / "data/instagram_runs/direct_profiles_no_company_ig_alternates_20260902.json",
]
CHANGED = ROOT / "data/instagram_runs/no_company_ig_verified_person_ids_20260902.json"

SELECTED = {
    447: ("shofujino", 5, ["matches_linkedin_identity", "role_in_bio"]),
    1008: ("mikeharounoff__", 5, ["matches_linkedin_identity", "role_in_bio"]),
    1007: ("moriahberger", 5, ["matches_linkedin_identity", "role_in_bio"]),
    1629: ("oskarjpekman", 6, ["matches_linkedin_identity", "company_in_bio"]),
}


def main() -> int:
    rows: list[dict] = []
    for path in AUDITS:
        rows.extend(json.loads(path.read_text(encoding="utf-8")))
    by_key = {(int(x["person_id"]), x["handle"].lower()): x for x in rows}
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    changed: list[int] = []
    try:
        for person_id, (handle, score, fired_names) in SELECTED.items():
            audit = by_key.get((person_id, handle.lower()))
            if not audit or not audit.get("text"):
                raise RuntimeError(f"Missing profile evidence for {person_id} @{handle}")
            person = con.execute(
                """SELECT p.full_name,p.current_verified,c.status AS company_status
                     FROM people p JOIN companies c USING(company_id)
                    WHERE p.person_id=? AND p.status!='REJECTED'""",
                (person_id,),
            ).fetchone()
            if not person or not person["current_verified"] or person["company_status"] != "QUALIFIED":
                raise RuntimeError(f"Person {person_id} no longer passes the hard gates")
            url = f"https://www.instagram.com/{handle}/"
            signals = {name: True for name in fired_names}
            signals["username_resembles_name"] = False
            private = "profile is private" in audit["text"].lower()
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
                (person_id, "instagram", url, handle, "VERIFIED", score,
                 "partial" if private else "full", json.dumps(signals, sort_keys=True),
                 stamp, stamp),
            )
            compact = " | ".join(line.strip() for line in audit["text"].splitlines() if line.strip())
            con.execute(
                """INSERT INTO person_evidence
                   (person_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(person_id,kind,url) DO UPDATE SET
                     source=excluded.source,snippet=excluded.snippet,
                     strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                (person_id, "profile", url, "instagram_signed_in_profile",
                 f"Exact displayed-name match to verified LinkedIn identity plus an independent SOP SS35 signal. Visible evidence: {compact[:900]}",
                 "strongest", stamp),
            )
            changed.append(person_id)
        con.commit()
    finally:
        con.close()
    CHANGED.write_text(json.dumps(sorted(changed)), encoding="utf-8")
    print(json.dumps({"verified": len(changed), "changed_ids": str(CHANGED)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
