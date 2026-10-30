"""Find additional LinkedIn decision-makers from public search results.

The search is restricted to already-qualified companies whose scoring evidence has
already produced at least one LinkedIn FINAL PASS.  Discovery evidence remains weak;
every retained URL must still be fetched and employer-verified by
``verify_profiles_batch.py`` before it can pass.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.normalize import normalize_company_name, normalize_person_name  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.sources import websearch  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402
from mp.stages.s5_people import _parse_linkedin_serp  # noqa: E402


LINKEDIN_PERSON = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+", re.I
)
ROLE_QUERIES = (
    "Founder",
    "President",
    "Director",
    "Head of",
    "Manager",
    "A&R",
)


def main(index: int, slices: int, mode: str) -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        company_filter = "" if mode == "all" else """
          AND EXISTS (
            SELECT 1 FROM people fp
            JOIN scores fs ON fs.person_id=fp.person_id AND fs.channel='linkedin'
            WHERE fp.company_id=c.company_id AND fp.status!='REJECTED'
              AND fs.verdict='FINAL PASS'
          )
        """
        rows = ctx.db.q(
            f"""SELECT c.company_id, c.canonical_name, c.roster_estimate,
                       SUM(CASE WHEN p.status!='REJECTED' THEN 1 ELSE 0 END) AS active
                FROM companies c
                LEFT JOIN people p ON p.company_id=c.company_id
                WHERE c.status='QUALIFIED' {company_filter}
                GROUP BY c.company_id
                HAVING active < 10
                ORDER BY c.company_id"""
        )
        rows = [r for i, r in enumerate(rows) if i % slices == index]
        print(f"slice {index}/{slices}: {len(rows)} companies ({mode})", flush=True)
        inserted = examined = duplicate_url = 0
        for row in rows:
            active = int(row["active"] or 0)
            before_company = inserted
            for role_term in ROLE_QUERIES:
                if active >= 10:
                    break
                query = f'"{row["canonical_name"]}" site:linkedin.com/in "{role_term}"'
                try:
                    results = websearch.search(ctx.http, query, limit=10)
                except Exception:
                    results = []
                for result in results:
                    if active >= 10:
                        break
                    examined += 1
                    match = LINKEDIN_PERSON.search(result.url or "")
                    if not match:
                        continue
                    url = match.group(0).split("?")[0].rstrip("/")
                    blob = f"{result.title} {result.snippet}"
                    expected = normalize_company_name(row["canonical_name"])
                    if not expected or expected not in normalize_company_name(blob):
                        continue
                    name, title = _parse_linkedin_serp(result.title, row["canonical_name"])
                    if not (name and title):
                        continue
                    role = classify_title(
                        title,
                        small_company=(row["roster_estimate"] is not None
                                       and row["roster_estimate"] <= 20),
                    )
                    if not role.qualifies:
                        continue
                    used = ctx.db.one(
                        """SELECT p.company_id FROM profiles pr
                           JOIN people p ON p.person_id=pr.person_id
                           WHERE pr.channel='linkedin' AND lower(rtrim(pr.url,'/'))=lower(?)
                           LIMIT 1""",
                        (url,),
                    )
                    if used:
                        duplicate_url += 1
                        continue
                    norm = normalize_person_name(name)
                    existing = ctx.db.one(
                        "SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
                        (row["company_id"], norm),
                    )
                    if existing:
                        continue
                    ctx.db.x(
                        """INSERT INTO people
                           (company_id, full_name, name_normalized, title, title_family,
                            seniority, role_relevance, status, first_seen)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            row["company_id"], name, norm, title, role.family,
                            role.seniority, role.relevance, "CANDIDATE", utcnow(),
                        ),
                    )
                    person = ctx.db.one(
                        "SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
                        (row["company_id"], norm),
                    )
                    pid = person["person_id"]
                    ctx.db.x(
                        """INSERT INTO profiles
                           (person_id, channel, url, handle, verification_status, retrieved_at)
                           VALUES (?,?,?,?,?,?)""",
                        (
                            pid, "linkedin", url, url.rsplit("/", 1)[-1],
                            "UNVERIFIED", utcnow(),
                        ),
                    )
                    ctx.db.x(
                        """INSERT INTO person_evidence
                           (person_id, kind, url, source, snippet, strength, retrieved_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (
                            pid, "employment", url, "websearch_linkedin",
                            blob[:1000], "weak", utcnow(),
                        ),
                    )
                    inserted += 1
                    active += 1
            print(
                f"  {row['canonical_name']}: +{inserted - before_company} (active {active})",
                flush=True,
            )
        print(
            f"slice {index}: inserted={inserted} examined={examined} "
            f"duplicate_urls={duplicate_url}",
            flush=True,
        )
        print(ctx.ledger.summary(), flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("index", type=int)
    parser.add_argument("slices", type=int)
    parser.add_argument("--mode", choices=("final", "all"), default="final")
    args = parser.parse_args()
    raise SystemExit(main(args.index, args.slices, args.mode))
