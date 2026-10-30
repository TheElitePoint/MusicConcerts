"""Enumerate SOP-relevant employees at already-qualified companies.

The actor is used only as a people/profile acquisition layer.  Company eligibility
has already been decided by the SOP pipeline.  Results are admitted only when the
reported *current* employer matches the target company and the current title passes
SS30/SS31.  A company can contribute no more than ten non-rejected LinkedIn people.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.normalize import (  # noqa: E402
    normalize_company_name,
    normalize_person_name,
)
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

_LEGAL_SUFFIXES = re.compile(
    r"\b(?:ltd|limited|llc|inc|incorporated|corp|corporation|gmbh|sas|srl|bv)\b",
    re.I,
)
_NON_PERSON = re.compile(
    r"\b(?:department|team|records|recordings|music|official|management|artist|"
    r"label|premiering|distribution|publishing)\b",
    re.I,
)


def _employer_key(value: str) -> str:
    return normalize_company_name(_LEGAL_SUFFIXES.sub(" ", value or ""))


def _human_name(value: str) -> bool:
    parts = [p for p in re.split(r"\s+", value.strip()) if p]
    return (
        2 <= len(parts) <= 7
        and not _NON_PERSON.search(value)
        and all(any(ch.isalpha() for ch in p) for p in parts)
    )


def _linkedin_company_url(item: dict) -> str:
    for key in ("currentPosition", "currentPositions"):
        entries = item.get(key) or []
        if entries and isinstance(entries, list) and isinstance(entries[0], dict):
            return entries[0].get("companyLinkedinUrl", "") or ""
    return ""


def _company_slug(value: str) -> str:
    match = re.search(r"linkedin\.com/company/([^/?#]+)", value or "", re.I)
    return match.group(1).lower() if match else ""


def _select_companies(ctx: Context, limit: int) -> list:
    # Prefer companies which already produced a FINAL person, then the strongest
    # small-roster/activity combinations.  The latter expands beyond the historical
    # company set while preserving every company gate from the SOP.
    return ctx.db.q(
        """SELECT c.company_id, c.canonical_name, c.linkedin_url,
                  c.roster_estimate,
                  (SELECT COUNT(DISTINCT a.signal_type)
                     FROM activity_signals a
                    WHERE a.company_id=c.company_id AND a.observed_at IS NOT NULL)
                      AS activity_categories,
                  (SELECT COUNT(*)
                     FROM people p JOIN scores s ON s.person_id=p.person_id
                    WHERE p.company_id=c.company_id AND s.channel='linkedin'
                      AND s.verdict='FINAL PASS') AS final_people,
                  (SELECT COUNT(*)
                     FROM people p JOIN profiles pr ON pr.person_id=p.person_id
                    WHERE p.company_id=c.company_id AND p.status!='REJECTED'
                      AND pr.channel='linkedin') AS active_linkedin
           FROM companies c
           WHERE c.status='QUALIFIED'
             AND (SELECT COUNT(*)
                    FROM people p JOIN scores s ON s.person_id=p.person_id
                   WHERE p.company_id=c.company_id AND s.channel='linkedin'
                     AND s.verdict='FINAL PASS') < 10
           ORDER BY (final_people > 0) DESC,
                    final_people DESC,
                    activity_categories DESC,
                    CASE WHEN c.type_confidence IN ('verified','strong','strongest')
                         THEN 0 ELSE 1 END,
                    CASE WHEN c.roster_estimate BETWEEN 3 AND 30 THEN 0 ELSE 1 END,
                    c.roster_estimate DESC,
                    c.company_id
           LIMIT ?""",
        (limit,),
    )


def _target_for_item(item: dict, by_slug: dict[str, dict], by_name: dict[str, list]) -> dict | None:
    slug = _company_slug(_linkedin_company_url(item))
    if slug and slug in by_slug:
        return by_slug[slug]
    company, _ = apify._position(item)
    matches = by_name.get(_employer_key(company), [])
    return matches[0] if len(matches) == 1 else None


def main(limit: int, batch_size: int, per_company: int) -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        rows = _select_companies(ctx, limit)
        print(f"company-employees: {len(rows)} qualified targets", flush=True)
        inserted = role_rejects = employer_rejects = duplicate_urls = 0

        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            by_slug = {
                _company_slug(r["linkedin_url"]): dict(r)
                for r in batch
                if _company_slug(r["linkedin_url"] or "")
            }
            by_name: dict[str, list[dict]] = defaultdict(list)
            for row in batch:
                by_name[_employer_key(row["canonical_name"])].append(dict(row))

            companies = [r["linkedin_url"] or r["canonical_name"] for r in batch]
            payload = {
                "profileScraperMode": "Short ($4 per 1k)",
                "companies": companies,
                "companyBatchMode": "one_by_one",
                "maxItemsPerCompany": per_company,
                "maxItems": per_company * len(companies),
                "takePages": 1,
            }
            ctx.ledger.require("apify", CONDITIONS)
            items = apify._run_actor(
                ctx.http,
                apify.ACTOR_COMPANY_EMPLOYEES,
                payload,
                context=f"company-employees:{start // batch_size + 1}",
            )
            print(
                f"  batch {start // batch_size + 1}: {len(companies)} companies, "
                f"{len(items)} profiles",
                flush=True,
            )

            admitted_by_company: dict[int, int] = defaultdict(int)
            for item in items:
                target = _target_for_item(item, by_slug, by_name)
                if target is None:
                    employer_rejects += 1
                    continue
                hit = apify._hits_from_harvestapi([item], target["canonical_name"])
                if not hit:
                    continue
                hit = hit[0]
                if not (_human_name(hit.full_name) and hit.linkedin_url):
                    continue
                if _employer_key(hit.company) != _employer_key(target["canonical_name"]):
                    employer_rejects += 1
                    continue
                role = classify_title(
                    hit.title or "",
                    small_company=(target["roster_estimate"] or 99) <= 20,
                )
                if not role.qualifies:
                    role_rejects += 1
                    continue
                capacity = max(0, 10 - int(target["active_linkedin"] or 0))
                if admitted_by_company[target["company_id"]] >= capacity:
                    continue
                if ctx.db.one(
                    "SELECT 1 FROM profiles WHERE channel='linkedin' AND lower(url)=lower(?)",
                    (hit.linkedin_url,),
                ):
                    duplicate_urls += 1
                    continue

                norm = normalize_person_name(hit.full_name)
                ctx.db.x(
                    """INSERT OR IGNORE INTO people
                       (company_id, full_name, name_normalized, title, title_family,
                        seniority, role_relevance, status, first_seen)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        target["company_id"], hit.full_name, norm, hit.title,
                        role.family, role.seniority, role.relevance,
                        "CANDIDATE", utcnow(),
                    ),
                )
                person = ctx.db.one(
                    "SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
                    (target["company_id"], norm),
                )
                if not person:
                    continue
                pid = person["person_id"]
                cur = ctx.db.x(
                    """INSERT OR IGNORE INTO profiles
                       (person_id, channel, url, handle, verification_status, retrieved_at)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        pid, "linkedin", hit.linkedin_url,
                        hit.linkedin_url.rstrip("/").rsplit("/", 1)[-1],
                        "UNVERIFIED", utcnow(),
                    ),
                )
                if not cur.rowcount:
                    continue
                ctx.db.x(
                    """INSERT INTO person_evidence
                       (person_id, kind, url, source, snippet, strength, retrieved_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(person_id, kind, url) DO UPDATE SET
                         source=excluded.source, snippet=excluded.snippet,
                         strength=excluded.strength, retrieved_at=excluded.retrieved_at""",
                    (
                        pid, "employment", hit.linkedin_url,
                        "apify_linkedin_company_employees",
                        f"LinkedIn current position: {hit.title} at {hit.company}",
                        "strongest", utcnow(),
                    ),
                )
                inserted += 1
                admitted_by_company[target["company_id"]] += 1

        print(
            f"inserted={inserted} role_rejects={role_rejects} "
            f"employer_rejects={employer_rejects} duplicate_urls={duplicate_urls}",
            flush=True,
        )
        print("apify:", ctx.ledger.summary().get("apify"), flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--per-company", type=int, default=25)
    args = parser.parse_args()
    raise SystemExit(main(args.limit, args.batch_size, args.per_company))
