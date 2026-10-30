"""Ingest signed-in browser Instagram verification evidence.

The browser pass confirms two independent SOP SS35 signals:
1. the personal profile's displayed full name exactly matches the verified
   LinkedIn identity; and
2. the official company Instagram account follows that personal profile.

Only directly opened, available profiles are ingested. Ambiguous, missing, or
name-mismatched rows remain in the research log and never enter FINAL.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLLOWING = ROOT / "data/instagram_runs/browser_following_20260830.json"
PROFILES = ROOT / "data/instagram_runs/browser_profile_checks_20260830.json"
CHANGED = ROOT / "data/instagram_runs/browser_verified_person_ids_20260830.json"


def now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    following = json.loads(FOLLOWING.read_text(encoding="utf-8"))
    checks = json.loads(PROFILES.read_text(encoding="utf-8"))
    checks_by_url = {row.get("person_instagram_url", "").rstrip("/").lower(): row for row in checks}

    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    changed: list[int] = []
    skipped: list[dict] = []
    timestamp = now()
    try:
        for row in following:
            if row.get("result") != "VERIFIED_COMPANY_FOLLOWS":
                continue
            url = row.get("person_instagram_url", "").rstrip("/") + "/"
            check = checks_by_url.get(url.rstrip("/").lower())
            if not check or check.get("profile_status") != "AVAILABLE" or not check.get("exact_name_on_profile"):
                skipped.append({"person": row.get("person"), "url": url, "reason": "direct profile check failed"})
                continue
            linkedin = (row.get("linkedin_url") or "").rstrip("/").lower()
            person = con.execute(
                """SELECT p.person_id,p.current_verified,c.canonical_name,c.instagram_url
                     FROM profiles li JOIN people p USING(person_id)
                     JOIN companies c USING(company_id)
                    WHERE li.channel='linkedin' AND lower(rtrim(li.url,'/'))=?
                      AND p.status!='REJECTED' AND p.current_verified=1
                      AND c.status='QUALIFIED'""",
                (linkedin,),
            ).fetchone()
            if not person:
                skipped.append({"person": row.get("person"), "url": url, "reason": "qualified LinkedIn identity not found"})
                continue

            signals = {
                "matches_linkedin_identity": True,
                "company_account_follows": True,
                "username_resembles_name": False,
            }
            usability = "partial" if check.get("is_private") else "full"
            identity_score = 6
            handle = row.get("person_handle") or url.rstrip("/").rsplit("/", 1)[-1]
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
                (person["person_id"], "instagram", url, handle, "VERIFIED", identity_score,
                 usability, json.dumps(signals, sort_keys=True), timestamp, timestamp),
            )
            company_url = row.get("company_instagram_url") or person["instagram_url"] or ""
            con.execute(
                """INSERT INTO person_evidence
                   (person_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(person_id,kind,url) DO UPDATE SET
                     source=excluded.source,snippet=excluded.snippet,
                     strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                (person["person_id"], "profile", company_url,
                 "official_company_instagram_following",
                 f"Official company Instagram @{row.get('company_handle')} follows @{handle}; the account display name exactly matches the verified LinkedIn identity {row.get('person')}.",
                 "strongest", timestamp),
            )
            con.execute(
                """INSERT INTO person_evidence
                   (person_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(person_id,kind,url) DO UPDATE SET
                     source=excluded.source,snippet=excluded.snippet,
                     strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                (person["person_id"], "profile", url, "instagram_signed_in_profile",
                 f"Signed-in direct profile check: exact display-name match; usability={usability}.",
                 "strongest", timestamp),
            )
            changed.append(int(person["person_id"]))
        con.commit()
    finally:
        con.close()

    CHANGED.write_text(json.dumps(sorted(set(changed))), encoding="utf-8")
    print(json.dumps({"verified": len(set(changed)), "skipped": len(skipped),
                      "changed_ids": str(CHANGED)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
