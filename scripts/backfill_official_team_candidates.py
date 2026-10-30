"""Backfill decision-makers from first-party team/about pages.

Only already-qualified companies are processed.  Candidates are ranked by SOP role
relevance and seniority, and no company is expanded beyond ten active people.
"""
from __future__ import annotations

import sys
import gzip
import json
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.normalize import normalize_person_name, registrable_domain  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402
from mp.sources import website  # noqa: E402


SENIORITY = {"executive": 0, "senior": 1, "mid_senior": 2, "limited": 3}


def main() -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        cached_pages: dict[str, list[tuple[str, str]]] = defaultdict(list)
        page_hint = re.compile(r"(?:^|/)(?:about|team|staff|people|contact|company|who-we-are)",
                               re.I)
        for path in (ROOT / "data/cache/http").glob("*/*.json.gz"):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    item = json.load(handle)
                url = item.get("url", "")
                text = item.get("text", "")
                if (int(item.get("status", 0)) == 200 and len(text) > 800
                        and page_hint.search(urlparse(url).path)):
                    domain = registrable_domain(url)
                    if domain:
                        cached_pages[domain].append((url, text))
            except (OSError, ValueError, TypeError):
                continue
        rows = ctx.db.q(
            """SELECT company_id, canonical_name, website, roster_estimate
               FROM companies
               WHERE status='QUALIFIED' AND website IS NOT NULL AND website!=''
               ORDER BY company_id"""
        )
        inserted = pages_with_people = 0
        for row in rows:
            active = ctx.db.one(
                "SELECT COUNT(*) n FROM people WHERE company_id=? AND status!='REJECTED'",
                (row["company_id"],),
            )["n"]
            if active >= 10:
                continue
            team = {}
            for page_url, html in cached_pages.get(registrable_domain(row["website"]), []):
                for person in website.extract_team(html, page_url):
                    team.setdefault(normalize_person_name(person.full_name), person)
            ranked = []
            for person in team.values():
                role = classify_title(
                    person.title,
                    small_company=(row["roster_estimate"] is not None
                                   and row["roster_estimate"] <= 20),
                )
                if role.qualifies:
                    ranked.append((-(role.relevance or 0),
                                   SENIORITY.get(role.seniority, 9),
                                   person, role))
            ranked.sort(key=lambda item: (item[0], item[1], item[2].full_name))
            if ranked:
                pages_with_people += 1
            before = inserted
            for _, _, person, role in ranked:
                if active >= 10:
                    break
                norm = normalize_person_name(person.full_name)
                if not norm or len(norm.split()) < 2:
                    continue
                existing = ctx.db.one(
                    "SELECT person_id,status FROM people WHERE company_id=? AND name_normalized=?",
                    (row["company_id"], norm),
                )
                if existing:
                    if existing["status"] != "REJECTED":
                        ctx.db.x(
                            """UPDATE people SET title=?, title_family=?, seniority=?,
                               role_relevance=? WHERE person_id=?""",
                            (person.title, role.family, role.seniority,
                             role.relevance, existing["person_id"]),
                        )
                        ctx.db.x(
                            """INSERT INTO person_evidence
                               (person_id,kind,url,source,snippet,strength,retrieved_at)
                               VALUES (?,?,?,?,?,?,?)
                               ON CONFLICT(person_id,kind,url) DO UPDATE SET
                                 source=excluded.source,snippet=excluded.snippet,
                                 strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                            (existing["person_id"], "employment", person.evidence_url,
                             "website_team", f"{person.full_name} - {person.title}",
                             "strongest", utcnow()),
                        )
                    continue
                ctx.db.x(
                    """INSERT INTO people
                       (company_id,full_name,name_normalized,title,title_family,seniority,
                        role_relevance,status,first_seen)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (row["company_id"], person.full_name, norm, person.title,
                     role.family, role.seniority, role.relevance, "CANDIDATE", utcnow()),
                )
                pid = ctx.db.one(
                    "SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
                    (row["company_id"], norm),
                )["person_id"]
                ctx.db.x(
                    """INSERT INTO person_evidence
                       (person_id,kind,url,source,snippet,strength,retrieved_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (pid, "employment", person.evidence_url, "website_team",
                     f"{person.full_name} - {person.title}", "strongest", utcnow()),
                )
                inserted += 1
                active += 1
            if inserted > before:
                print(f"{row['canonical_name']}: +{inserted-before} (active {active})",
                      flush=True)
        print(f"official team pages={pages_with_people} inserted={inserted}", flush=True)
        print(ctx.ledger.summary(), flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
