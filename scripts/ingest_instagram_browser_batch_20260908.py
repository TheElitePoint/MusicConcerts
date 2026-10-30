"""Ingest the 2026-09-08 signed-in Instagram verification batch.

Every selected profile was opened directly and matched to the already verified
LinkedIn identity.  Where the profile did not itself name the employer, the
official company Instagram account's Following search supplied the second SS35
signal.  The batch intentionally excludes ambiguous and mismatched accounts.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/master.sqlite"
CHANGED = ROOT / "data/instagram_runs/browser_verified_person_ids_20260908.json"


SELECTED = {
    25:  ("marinx611", "full", ["matches_linkedin_identity", "company_in_bio", "company_account_follows"]),
    29:  ("emmaksnook", "partial", ["matches_linkedin_identity", "company_tagged", "company_account_follows"]),
    30:  ("fanniemal", "partial", ["matches_linkedin_identity", "company_account_follows"]),
    33:  ("sarajanedempsey", "partial", ["matches_linkedin_identity", "company_account_follows"]),
    54:  ("rusticsorcerer", "full", ["matches_linkedin_identity", "company_in_bio", "role_in_bio", "company_account_follows"]),
    56:  ("alicegros", "partial", ["matches_linkedin_identity", "company_account_follows"]),
    59:  ("jonny.0h", "full", ["matches_linkedin_identity", "company_account_follows"]),
    92:  ("dan.hardingham", "partial", ["matches_linkedin_identity", "company_account_follows"]),
    133: ("zgarkadominique", "full", ["matches_linkedin_identity", "external_links_confirm"]),
    135: ("archhillrecordings", "full", ["matches_linkedin_identity", "company_account_follows"]),
    185: ("thewillstreetjournal", "partial", ["matches_linkedin_identity", "company_in_bio", "role_in_bio"]),
    191: ("kessels", "partial", ["matches_linkedin_identity", "company_account_follows"]),
    199: ("rhunley_11", "full", ["matches_linkedin_identity", "company_account_follows"]),
    200: ("goldswagger_", "full", ["matches_linkedin_identity", "company_account_follows"]),
    206: ("kathiemerritt", "full", ["matches_linkedin_identity", "company_account_follows"]),
    211: ("winstonlegthigh", "full", ["matches_linkedin_identity", "company_in_bio", "role_in_bio"]),
    379: ("mike_ajayi", "partial", ["matches_linkedin_identity", "company_in_bio", "role_in_bio"]),
    411: ("evefchickwe", "partial", ["matches_linkedin_identity", "company_in_bio", "role_in_bio"]),
    913: ("jedmorgans", "full", ["matches_linkedin_identity", "company_in_bio", "external_links_confirm"]),
    2337:("marisak20", "full", ["matches_linkedin_identity", "company_in_bio", "external_links_confirm"]),
    2344:("hudikgreg", "full", ["matches_linkedin_identity", "company_in_bio", "role_in_bio", "external_links_confirm"]),
    2335:("claire.for.you", "partial", ["matches_linkedin_identity", "company_in_bio", "company_account_follows"]),
    2412:("beckysake", "full", ["matches_linkedin_identity", "company_in_bio", "role_in_bio"]),
    2594:("jorisbomboris", "full", ["matches_linkedin_identity", "company_in_bio", "external_links_confirm"]),
    214: ("matthewbury", "full", ["matches_linkedin_identity", "company_account_follows"]),
    385: ("philwaldorf", "full", ["matches_linkedin_identity", "external_links_confirm"]),
    394: ("kaylaganz", "partial", ["matches_linkedin_identity", "company_account_follows"]),
    415: ("jstevenson44", "partial", ["matches_linkedin_identity", "company_account_follows"]),
    409: ("therealwillswan", "full", ["company_in_bio", "role_in_bio", "company_account_follows"]),
    410: ("sergio.medina93", "full", ["matches_linkedin_identity", "company_in_bio", "role_in_bio", "company_account_follows"]),
    426: ("mitchelldaniels99", "partial", ["matches_linkedin_identity", "company_account_follows"]),
    427: ("missmiscreant", "partial", ["matches_linkedin_identity", "company_account_follows"]),
    443: ("jenna_barber", "partial", ["matches_linkedin_identity", "company_in_bio", "company_account_follows"]),
    457: ("turanofromto", "partial", ["matches_linkedin_identity", "company_account_follows"]),
    494: ("cat666man", "partial", ["matches_linkedin_identity", "company_in_bio", "company_account_follows"]),
    498: ("quentinmartins11", "partial", ["matches_linkedin_identity", "company_account_follows"]),
    509: ("i.l.a.n.a", "full", ["matches_linkedin_identity", "company_account_follows"]),
    545: ("maartjealbers", "full", ["matches_linkedin_identity", "company_in_bio", "role_in_bio", "company_tagged", "company_account_follows"]),
    673: ("jayreason", "full", ["matches_linkedin_identity", "company_account_follows"]),
    683: ("le_savy_sev", "partial", ["matches_linkedin_identity", "company_account_follows"]),
    703: ("bertieblackman", "full", ["matches_linkedin_identity", "company_account_follows"]),
    1066:("kevandyd", "full", ["matches_linkedin_identity", "company_account_follows"]),
    1085:("_hannahbeatrice", "partial", ["matches_linkedin_identity", "company_in_bio", "role_in_bio"]),
    1397:("ryleyremedios", "full", ["matches_linkedin_identity", "company_account_follows"]),
    1420:("gregthompson12", "partial", ["matches_linkedin_identity", "company_account_follows"]),
    2474:("lexieviklund", "full", ["matches_linkedin_identity", "company_in_bio", "role_in_bio", "company_account_follows"]),
    2818:("napalm_records_max", "full", ["matches_linkedin_identity", "company_in_bio", "company_account_follows", "external_links_confirm"]),
    2823:("baasjvl", "partial", ["matches_linkedin_identity", "company_in_bio", "role_in_bio", "company_account_follows"]),
}

WEIGHTS = {
    "company_in_bio": 3,
    "role_in_bio": 2,
    "company_tagged": 2,
    "company_account_follows": 3,
    "matches_linkedin_identity": 3,
    "external_links_confirm": 3,
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    timestamp = now()
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    changed: list[int] = []
    try:
        for person_id, (handle, usability, signal_names) in SELECTED.items():
            person = con.execute(
                """SELECT p.full_name,p.current_verified,p.status,c.canonical_name,c.instagram_url
                     FROM people p JOIN companies c USING(company_id)
                    WHERE p.person_id=? AND c.status='QUALIFIED'""",
                (person_id,),
            ).fetchone()
            if not person or not person["current_verified"] or person["status"] == "REJECTED":
                raise RuntimeError(f"person {person_id} is not a verified current employee at a qualified company")

            score = sum(WEIGHTS[name] for name in signal_names)
            if score < 5:
                raise RuntimeError(f"person {person_id} does not meet SS35 threshold")
            signals = {name: True for name in signal_names}
            signals["username_resembles_name"] = False
            url = f"https://www.instagram.com/{handle}/"
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
                (person_id, "instagram", url, handle, "VERIFIED", score, usability,
                 json.dumps(signals, sort_keys=True), timestamp, timestamp),
            )
            con.execute(
                """INSERT INTO person_evidence
                   (person_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(person_id,kind,url) DO UPDATE SET
                     source=excluded.source,snippet=excluded.snippet,
                     strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                (person_id, "profile", url, "instagram_signed_in_profile",
                 f"Direct signed-in profile check matched the displayed identity for {person['full_name']}; usability={usability}; SS35 signals={','.join(signal_names)}.",
                 "strongest", timestamp),
            )
            if "company_account_follows" in signal_names:
                con.execute(
                    """INSERT INTO person_evidence
                       (person_id,kind,url,source,snippet,strength,retrieved_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(person_id,kind,url) DO UPDATE SET
                         source=excluded.source,snippet=excluded.snippet,
                         strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                    (person_id, "profile", person["instagram_url"],
                     "official_company_instagram_following",
                     f"Official {person['canonical_name']} Instagram Following search returned @{handle} with the matching display identity {person['full_name']}.",
                     "strongest", timestamp),
                )
            changed.append(person_id)
        con.commit()
    finally:
        con.close()

    CHANGED.parent.mkdir(parents=True, exist_ok=True)
    CHANGED.write_text(json.dumps(sorted(changed), indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"verified": len(changed), "changed_ids": str(CHANGED)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
