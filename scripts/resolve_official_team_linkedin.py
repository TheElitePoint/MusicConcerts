"""Resolve LinkedIn URLs for first-party official-team candidates only."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.normalize import normalize_company_name  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402
from mp.sources import apify  # noqa: E402
from mp.stages.s6_profiles import _free_lookup, _store  # noqa: E402


CONDITIONS = {
    "company_passed_all_gates": True,
    "person_has_verified_name": True,
    "person_title_in_role_family": True,
    "no_cached_profile": True,
    "free_sources_exhausted": True,
}


def employer_matches(expected: str, found: str) -> bool:
    a, b = normalize_company_name(expected), normalize_company_name(found)
    return bool(a and b and (a == b or (len(a) >= 8 and a in b)
                            or (len(b) >= 8 and b in a)))


def main() -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        rows = ctx.db.q(
            """SELECT DISTINCT p.person_id,p.full_name,p.title,c.canonical_name,
                              c.roster_estimate
               FROM people p
               JOIN companies c ON c.company_id=p.company_id
               JOIN person_evidence pe ON pe.person_id=p.person_id
               WHERE c.status='QUALIFIED' AND p.status!='REJECTED'
                 AND pe.source='website_team'
                 AND NOT EXISTS (SELECT 1 FROM profiles pr
                                 WHERE pr.person_id=p.person_id
                                   AND pr.channel='linkedin')
               ORDER BY p.role_relevance DESC,p.person_id"""
        )
        print(f"official-team candidates needing LinkedIn: {len(rows)}", flush=True)
        found = free = paid = title_reject = mismatch = 0
        for i, row in enumerate(rows, 1):
            url, evidence = _free_lookup(
                ctx, row["full_name"], row["canonical_name"], "linkedin"
            )
            source = "websearch"
            hit = None
            if url:
                free += 1
            elif apify.available(ctx.http):
                hit = apify.find_linkedin_profile(
                    ctx.http, row["full_name"], row["canonical_name"],
                    conditions=CONDITIONS,
                )
                if hit.found:
                    url, source = hit.linkedin_url, "apify"
                    paid += 1
            if hit and hit.found:
                if not employer_matches(row["canonical_name"], hit.company or ""):
                    mismatch += 1
                    continue
                role = classify_title(
                    hit.title or "",
                    small_company=(row["roster_estimate"] is not None
                                   and row["roster_estimate"] <= 20),
                )
                if not role.qualifies:
                    ctx.db.x(
                        "UPDATE people SET status='REJECTED',reject_reason=? WHERE person_id=?",
                        (role.exclusion_reason or "Current LinkedIn title is not an SOP role",
                         row["person_id"]),
                    )
                    title_reject += 1
                    continue
                ctx.db.x(
                    """UPDATE people SET title=?,title_family=?,seniority=?,role_relevance=?
                       WHERE person_id=?""",
                    (hit.title, role.family, role.seniority, role.relevance,
                     row["person_id"]),
                )
                ctx.db.x(
                    """INSERT INTO person_evidence
                       (person_id,kind,url,source,snippet,strength,retrieved_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(person_id,kind,url) DO UPDATE SET
                         source=excluded.source,snippet=excluded.snippet,
                         strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                    (row["person_id"], "employment", url, "apify_linkedin",
                     f"LinkedIn current position: {hit.title} at {hit.company}",
                     "strongest", utcnow()),
                )
            if url:
                _store(ctx, row["person_id"], "linkedin", url, evidence, source)
                found += 1
            if i % 10 == 0:
                print(f"  {i}/{len(rows)} found={found}", flush=True)
        print(f"found={found} free={free} paid={paid} title_reject={title_reject} "
              f"employer_mismatch={mismatch}", flush=True)
        print(ctx.ledger.summary().get("apify"), flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
