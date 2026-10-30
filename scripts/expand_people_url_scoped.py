"""Expand qualified companies through URL-scoped LinkedIn employee search.

This is deliberately stricter than the generic-name sweeps:
* the company must already be QUALIFIED and have a verified LinkedIn company URL;
* LinkedIn's reported current employer must match the canonical company name;
* only SS30/SS31 role families are retained;
* no company is expanded beyond ten non-rejected people in this run.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.normalize import normalize_company_name, normalize_person_name  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.sources import apify  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402


CONDITIONS = {
    "company_passed_all_gates": True,
    "person_has_verified_name": True,
    "person_title_in_role_family": True,
    "no_cached_profile": True,
    "free_sources_exhausted": True,
}


def employer_matches(expected: str, found: str) -> bool:
    a = normalize_company_name(expected)
    b = normalize_company_name(found)
    if not (a and b):
        return False
    return a == b or (len(a) >= 8 and a in b) or (len(b) >= 8 and b in a)


def main(actor_key: str, limit: int) -> int:
    actor = {
        "neural": apify.ACTOR_PEOPLE_SEARCH,
        "harvest": apify.ACTOR_PEOPLE_SEARCH_FALLBACK,
    }[actor_key]
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        rows = ctx.db.q(
            """SELECT c.company_id, c.canonical_name, c.linkedin_url,
                      c.roster_estimate,
                      SUM(CASE WHEN p.status != 'REJECTED' THEN 1 ELSE 0 END) AS active
               FROM companies c
               LEFT JOIN people p ON p.company_id=c.company_id
               WHERE c.status='QUALIFIED'
                 AND c.linkedin_url IS NOT NULL AND c.linkedin_url!=''
               GROUP BY c.company_id
               HAVING active < 10
               ORDER BY active ASC,
                        CASE WHEN c.roster_estimate BETWEEN 3 AND 20 THEN 0 ELSE 1 END,
                        c.roster_estimate DESC, c.company_id
               LIMIT ?""",
            (limit,),
        )
        print(f"[{actor_key}] {len(rows)} URL-scoped qualified companies", flush=True)
        calls = inserted = matched = rejected_role = rejected_employer = 0
        for row in rows:
            available = max(0, 10 - int(row["active"] or 0))
            if not available:
                continue
            if actor == apify.ACTOR_PEOPLE_SEARCH:
                payload = {"companies": [row["linkedin_url"]], "maxResults": 25}
            else:
                payload = {
                    "profileScraperMode": "Short",
                    "maxItems": 25,
                    "takePages": 1,
                    "currentCompanies": [row["linkedin_url"]],
                }
            try:
                ctx.ledger.require("apify", CONDITIONS)
                items = apify._run_actor(
                    ctx.http,
                    actor,
                    payload,
                    context=f"url-expand:{actor_key}:{row['canonical_name']}",
                )
            except Exception as exc:
                print(f"  skip {row['canonical_name']}: {type(exc).__name__}", flush=True)
                continue
            calls += 1
            hits = (
                apify._hits_from_neuralverge(items, row["canonical_name"])
                if actor == apify.ACTOR_PEOPLE_SEARCH
                else apify._hits_from_harvestapi(items, row["canonical_name"])
            )
            for hit in hits:
                if available <= 0:
                    break
                if not (hit.found and hit.full_name and hit.linkedin_url):
                    continue
                if not employer_matches(row["canonical_name"], hit.company or ""):
                    rejected_employer += 1
                    continue
                matched += 1
                role = classify_title(
                    hit.title or "", small_company=(row["roster_estimate"] or 99) <= 20
                )
                if not role.qualifies:
                    rejected_role += 1
                    continue
                norm = normalize_person_name(hit.full_name)
                before = ctx.db.one(
                    "SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
                    (row["company_id"], norm),
                )
                ctx.db.x(
                    """INSERT OR IGNORE INTO people
                       (company_id, full_name, name_normalized, title, title_family,
                        seniority, role_relevance, status, first_seen)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        row["company_id"], hit.full_name, norm, hit.title,
                        role.family, role.seniority, role.relevance, "CANDIDATE", utcnow(),
                    ),
                )
                person = ctx.db.one(
                    "SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
                    (row["company_id"], norm),
                )
                if not person:
                    continue
                pid = person["person_id"]
                ctx.db.x(
                    """INSERT OR IGNORE INTO profiles
                       (person_id, channel, url, handle, verification_status, retrieved_at)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        pid, "linkedin", hit.linkedin_url,
                        hit.linkedin_url.rstrip("/").rsplit("/", 1)[-1],
                        "UNVERIFIED", utcnow(),
                    ),
                )
                ctx.db.x(
                    """INSERT INTO person_evidence
                       (person_id, kind, url, source, snippet, strength, retrieved_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(person_id, kind, url) DO UPDATE SET
                         source=excluded.source, snippet=excluded.snippet,
                         strength=excluded.strength, retrieved_at=excluded.retrieved_at""",
                    (
                        pid, "employment", hit.linkedin_url, "apify_linkedin",
                        f"LinkedIn current position: {hit.title} at {hit.company}",
                        "strongest", utcnow(),
                    ),
                )
                if before is None:
                    inserted += 1
                    available -= 1
            print(
                f"  {row['canonical_name']}: active {row['active']} -> "
                f"{10 - available}",
                flush=True,
            )
        print(
            f"[{actor_key}] calls={calls} inserted={inserted} matched={matched} "
            f"role_rejects={rejected_role} employer_rejects={rejected_employer}",
            flush=True,
        )
        print(ctx.ledger.summary().get("apify"), flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("actor", choices=("neural", "harvest"))
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    raise SystemExit(main(args.actor, args.limit))
