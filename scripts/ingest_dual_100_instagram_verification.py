"""Ingest SOP-verified Instagram profiles from the 2026-09-02 browser audit.

Every selected profile has an exact displayed-name match to the already verified
LinkedIn identity plus at least one additional SS35 signal. Profiles with a
visible conflicting employer/industry were deliberately excluded.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIMARY = ROOT / "data/instagram_runs/direct_profiles_dual_100_20260902.json"
SECOND = ROOT / "data/instagram_runs/direct_profiles_alternates_dual_100_20260902.json"
FOLLOWING = ROOT / "data/instagram_runs/browser_following_dual_100_20260902.json"
CHANGED = ROOT / "data/instagram_runs/dual_100_instagram_verified_person_ids_20260902.json"


# Explicit human-reviewed selections from the signed-in profile audit. The
# signal lists use the SOP SS35 names; identity_score is the applicable weight
# sum. company_account_follows is the SOP official-company-follow signal.
SELECTED = {
    195: ("jules_lg_", 7, ["matches_linkedin_identity", "role_in_bio", "company_tagged"]),
    192: ("claretastic", 7, ["matches_linkedin_identity", "role_in_bio", "company_tagged"]),
    686: ("allieapple808", 5, ["matches_linkedin_identity", "role_in_bio"]),
    385: ("philwaldorf", 6, ["matches_linkedin_identity", "company_account_follows"]),
    175: ("ajtobey", 6, ["matches_linkedin_identity", "company_in_bio"]),
    1288: ("72matt", 5, ["matches_linkedin_identity", "company_tagged"]),
    187: ("jessifrick", 5, ["matches_linkedin_identity", "company_tagged"]),
    11: ("berndrieger_", 5, ["matches_linkedin_identity", "company_tagged"]),
    1417: ("wesdone", 7, ["matches_linkedin_identity", "role_in_bio", "company_tagged"]),
    1411: ("nateyetton", 7, ["matches_linkedin_identity", "role_in_bio", "company_tagged"]),
    545: ("maartjealbers", 7, ["matches_linkedin_identity", "role_in_bio", "company_tagged"]),
    509: ("i.l.a.n.a", 6, ["matches_linkedin_identity", "company_account_follows"]),
    378: ("maxlavinsky", 5, ["matches_linkedin_identity", "company_tagged"]),
    341: ("nadworks", 5, ["matches_linkedin_identity", "role_in_bio"]),
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    audits = json.loads(PRIMARY.read_text(encoding="utf-8"))
    audits += json.loads(SECOND.read_text(encoding="utf-8"))
    by_key = {(int(x["person_id"]), x["handle"].lower()): x for x in audits}
    following = json.loads(FOLLOWING.read_text(encoding="utf-8"))
    follow_by_person = {
        int(x["person_id"]): x for x in following
        if x.get("result") == "VERIFIED_COMPANY_FOLLOWS"
    }

    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    stamp = now()
    changed: list[int] = []
    try:
        for person_id, (handle, identity_score, fired_names) in SELECTED.items():
            audit = by_key.get((person_id, handle.lower()))
            if not audit or not audit.get("text"):
                raise RuntimeError(f"Missing direct profile evidence for {person_id} @{handle}")
            person = con.execute(
                """SELECT p.full_name,p.current_verified,c.canonical_name,c.status AS company_status,
                          c.instagram_url AS company_instagram_url
                     FROM people p JOIN companies c USING(company_id)
                    WHERE p.person_id=? AND p.status!='REJECTED'""",
                (person_id,),
            ).fetchone()
            if not person or not person["current_verified"] or person["company_status"] != "QUALIFIED":
                raise RuntimeError(f"Person {person_id} no longer passes the LinkedIn/company gates")

            url = f"https://www.instagram.com/{handle}/"
            is_private = "profile is private" in audit["text"].lower()
            signals = {name: True for name in fired_names}
            signals["username_resembles_name"] = False
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
                (person_id, "instagram", url, handle, "VERIFIED", identity_score,
                 "partial" if is_private else "full", json.dumps(signals, sort_keys=True),
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
                 f"Exact displayed-name match to verified LinkedIn identity. Visible profile evidence: {compact[:900]}",
                 "strongest", stamp),
            )
            if person_id in follow_by_person:
                follow = follow_by_person[person_id]
                company_url = follow.get("company_instagram_url") or person["company_instagram_url"] or ""
                con.execute(
                    """INSERT INTO person_evidence
                       (person_id,kind,url,source,snippet,strength,retrieved_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(person_id,kind,url) DO UPDATE SET
                         source=excluded.source,snippet=excluded.snippet,
                         strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                    (person_id, "profile", company_url,
                     "official_company_instagram_following",
                     f"Official {person['canonical_name']} Instagram account follows @{handle}; the account display name matches {person['full_name']}.",
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
