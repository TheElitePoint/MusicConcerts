"""Ingest the incremental signed-in Instagram verifications from 2026-09-09."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/master.sqlite"

SELECTED = {
    136: {
        "handle": "harietellis",
        "usability": "partial",
        "signals": ["company_in_bio", "company_account_follows"],
        "snippet": "Private @harietellis profile uses the exact verified name as its handle, tags @flyingnun in the bio, and is followed by official @flyingnun.",
    },
    572: {
        "handle": "pedro_vian",
        "usability": "full",
        "signals": ["matches_linkedin_identity", "company_in_bio"],
        "snippet": "Exact public profile for Pedro Vian; bio identifies him as a music composer and tags @modernobscuremusic and @mom.publishing.",
    },
    208: {
        "handle": "suelukebr",
        "usability": "full",
        "signals": ["matches_linkedin_identity", "company_in_bio", "role_in_bio", "company_account_follows"],
        "snippet": "Exact public profile for Sue Lucarelli states President @epitaphrecords; official @epitaphrecords follows @suelukebr.",
    },
    212: {
        "handle": "felicia.risolo",
        "usability": "full",
        "signals": ["matches_linkedin_identity", "company_in_bio", "role_in_bio", "company_account_follows"],
        "snippet": "Exact public profile for Felicia Risolo states Marketing @epitaphrecords; official @epitaphrecords follows @felicia.risolo.",
    },
    360: {
        "handle": "kindoflikelisa",
        "usability": "full",
        "signals": ["matches_linkedin_identity", "company_in_bio", "company_account_follows"],
        "snippet": "Exact public profile for Lisa Garelick tags @epitaphrecords in the bio; official @epitaphrecords follows @kindoflikelisa.",
    },
    180: {
        "handle": "zakbenjamin",
        "usability": "partial",
        "signals": ["matches_linkedin_identity", "company_account_follows"],
        "company_instagram_url": "https://www.instagram.com/cr2records/",
        "snippet": "Exact display name Zak Benjamin; official @cr2records Following search returns @zakbenjamin; private but messageable profile.",
    },
    349: {
        "handle": "alex_knezevic_",
        "usability": "partial",
        "signals": ["matches_linkedin_identity", "company_account_follows"],
        "company_instagram_url": "https://www.instagram.com/farout_recordings/",
        "snippet": "Exact display name Alex Knezevic; official @farout_recordings Following search returns @alex_knezevic_; private but messageable profile.",
    },
    388: {
        "handle": "rachelglago",
        "usability": "full",
        "signals": ["company_in_bio", "company_account_follows"],
        "snippet": "Public @rachelglago profile has the distinctive first-name/surname handle, tags @secretly, @secretlycanadian, @jagjaguwar and @deadoceans in the bio, and official @deadoceans follows it.",
    },
    1009: {
        "handle": "_rishisaluja",
        "usability": "full",
        "signals": ["matches_linkedin_identity", "company_account_follows"],
        "company_instagram_url": "https://www.instagram.com/transgressivehq/",
        "snippet": "Exact display name Rishi Saluja on an available public profile; official @transgressivehq Following search returns @_rishisaluja.",
    },
    1018: {
        "handle": "jacknevercrashes",
        "usability": "partial",
        "signals": ["matches_linkedin_identity", "company_in_bio", "role_in_bio", "company_account_follows"],
        "company_instagram_url": "https://www.instagram.com/transgressivehq/",
        "snippet": "Exact display name Jack Hedges; bio states GM @transgressivehq North America; official @transgressivehq Following search returns @jacknevercrashes; private but messageable profile.",
    },
    1010: {
        "handle": "hayley_abs",
        "usability": "partial",
        "signals": ["matches_linkedin_identity", "company_in_bio"],
        "snippet": "Exact display name Hayley Absalom; bio tags @transgressivehq; private but messageable profile.",
    },
    2820: {
        "handle": "jonathanrego",
        "usability": "full",
        "signals": ["matches_linkedin_identity", "company_account_follows"],
        "snippet": "Exact display name Jonathan Rego on an available public profile; official @fearlessrecords Following search returns @jonathanrego.",
    },
}

WEIGHTS = {
    "company_in_bio": 3,
    "role_in_bio": 2,
    "company_account_follows": 3,
    "matches_linkedin_identity": 3,
}


def main() -> int:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con = sqlite3.connect(DB, timeout=60)
    try:
        for person_id, item in SELECTED.items():
            row = con.execute(
                """SELECT p.current_verified,p.status,c.status,c.instagram_url,c.canonical_name
                     FROM people p JOIN companies c USING(company_id)
                    WHERE p.person_id=?""",
                (person_id,),
            ).fetchone()
            if not row or row[0] != 1 or row[1] == "REJECTED" or row[2] != "QUALIFIED":
                raise RuntimeError(f"person {person_id} no longer meets verified-current gates")
            handle = item["handle"]
            url = f"https://www.instagram.com/{handle}/"
            signal_names = item["signals"]
            identity_score = sum(WEIGHTS[name] for name in signal_names)
            signals = {name: True for name in signal_names}
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
                 item["usability"], json.dumps(signals, sort_keys=True), timestamp, timestamp),
            )
            con.execute(
                """INSERT INTO person_evidence
                   (person_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(person_id,kind,url) DO UPDATE SET
                     source=excluded.source,snippet=excluded.snippet,
                     strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                (person_id, "profile", url, "instagram_signed_in_profile",
                 item["snippet"], "strongest", timestamp),
            )
            if "company_account_follows" in signal_names:
                con.execute(
                    """INSERT INTO person_evidence
                       (person_id,kind,url,source,snippet,strength,retrieved_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(person_id,kind,url) DO UPDATE SET
                         source=excluded.source,snippet=excluded.snippet,
                         strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                    (person_id, "profile", item.get("company_instagram_url") or row[3], "official_company_instagram_following",
                     f"Official {row[4]} Instagram Following search returned @{handle} with the exact matching identity.",
                     "strongest", timestamp),
                )
        con.commit()
    finally:
        con.close()
    print(json.dumps({"verified_person_ids": sorted(SELECTED)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
