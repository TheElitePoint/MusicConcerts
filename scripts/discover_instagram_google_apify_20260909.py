"""Find direct Instagram profile candidates with company-scoped Google searches.

This is discovery only: results are written to an audit file and are not inserted
into the database or exported.  A candidate must still pass the SOP identity and
employment checks before it can become FINAL PASS.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config  # noqa: E402
from mp.db import Database  # noqa: E402


ACTOR_URL = (
    "https://api.apify.com/v2/acts/apify~google-search-scraper/"
    "run-sync-get-dataset-items?format=json&clean=true&timeout=300"
)
BLOCKED_PATHS = {
    "p", "reel", "reels", "stories", "explore", "accounts", "direct",
    "about", "developer", "tv", "challenge", "web", "legal",
}

# Rows manually flagged red in the live sheet after an entity-resolution audit.
# These identities belong to a different same/similar-name company, or to an
# out-of-scope role, and must never seed Instagram discovery.
RELEVANCE_EXCLUDED_PERSON_IDS = {
    172, 383, 384, 501, 502, 748, 1646, 1791, 1792, 1876, 2469, 2470, 2471,
}


def query(person: str, company: str, query_mode: str = "company") -> str:
    person = person.replace('"', " ").strip()
    company = company.replace('"', " ").strip()
    if query_mode == "name":
        return f'site:instagram.com/ "{person}"'
    if query_mode == "role":
        return f'site:instagram.com/ "{person}" (label OR records OR music)'
    return f'site:instagram.com/ "{person}" "{company}"'


def direct_profile_url(value: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    if parsed.netloc.lower() not in {"instagram.com", "www.instagram.com"}:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 1 or parts[0].lower() in BLOCKED_PATHS:
        return ""
    handle = parts[0]
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle):
        return ""
    return f"https://www.instagram.com/{handle}/"


def targets(limit: int, offset: int, min_linkedin_score: int,
            allow_linkedin_review: bool) -> list[dict]:
    con = Database(ROOT / "data/master.sqlite")
    try:
        rows = con.q(
            """SELECT p.person_id,p.full_name,p.title,c.canonical_name AS company,
                      c.instagram_url AS company_instagram_url,s.total AS linkedin_score
                 FROM people p
                 JOIN companies c USING(company_id)
                 JOIN scores s ON s.person_id=p.person_id AND s.channel='linkedin'
                WHERE p.current_verified=1 AND p.status!='REJECTED'
                  AND c.status='QUALIFIED'
                  AND ((?=0 AND s.verdict='FINAL PASS')
                       OR (?=1 AND s.verdict!='FINAL PASS'))
                  AND s.total>=?
                  AND p.person_id NOT IN ({excluded_ids})
                  AND NOT EXISTS (
                      SELECT 1 FROM profiles ig WHERE ig.person_id=p.person_id
                        AND ig.channel='instagram' AND ig.verification_status='VERIFIED')
                ORDER BY s.total DESC,p.role_relevance DESC,p.person_id
                LIMIT ? OFFSET ?""".format(
                    excluded_ids=",".join("?" for _ in RELEVANCE_EXCLUDED_PERSON_IDS)
                ),
            (1 if allow_linkedin_review else 0, 1 if allow_linkedin_review else 0,
             min_linkedin_score,
             *sorted(RELEVANCE_EXCLUDED_PERSON_IDS), limit, offset),
        )
        return [dict(row) for row in rows]
    finally:
        con.close()


def main(limit: int, batch_size: int, offset: int, min_linkedin_score: int,
         query_mode: str, allow_linkedin_review: bool) -> int:
    tokens = config.keys("apify")
    if not tokens:
        raise SystemExit("No Apify token configured")
    token = tokens[-1]
    rows = targets(limit, offset, min_linkedin_score, allow_linkedin_review)
    all_results: list[dict] = []
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "data" / "instagram_runs" / f"google_profile_candidates_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / "candidates.json"
    for offset in range(0, len(rows), batch_size):
        batch = rows[offset:offset + batch_size]
        queries = [query(row["full_name"], row["company"], query_mode) for row in batch]
        response = requests.post(
            ACTOR_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "queries": "\n".join(queries),
                "maxPagesPerQuery": 1,
                "mobileResults": False,
                "includeUnfilteredResults": False,
                "saveHtml": False,
                "saveHtmlToKeyValueStore": False,
                "includeIcons": False,
            },
            timeout=360,
        )
        response.raise_for_status()
        pages = response.json()
        by_term = {}
        for page in pages if isinstance(pages, list) else []:
            search = page.get("searchQuery") or {}
            term = str(search.get("term") or search.get("query") or page.get("query") or "")
            by_term[term] = page
        for row, term in zip(batch, queries):
            page = by_term.get(term) or {}
            found = []
            for hit in page.get("organicResults") or []:
                url = direct_profile_url(str(hit.get("url") or hit.get("link") or ""))
                if not url:
                    continue
                found.append({
                    "url": url,
                    "title": str(hit.get("title") or ""),
                    "snippet": str(hit.get("description") or hit.get("snippet") or ""),
                })
            all_results.append({**row, "query": term, "candidates": found})
        output.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"searched={min(offset + len(batch), len(rows))}/{len(rows)}", flush=True)

    output.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")
    hits = sum(bool(row["candidates"]) for row in all_results)
    print(json.dumps({"searched": len(rows), "people_with_candidates": hits, "output": str(output)}))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--min-linkedin-score", type=int, default=90)
    parser.add_argument("--query-mode", choices=("company", "name", "role"), default="company")
    parser.add_argument("--allow-linkedin-review", action="store_true")
    args = parser.parse_args()
    raise SystemExit(main(
        max(1, args.limit), max(1, min(args.batch_size, 100)), max(0, args.offset),
        max(0, min(args.min_linkedin_score, 100)), args.query_mode,
        args.allow_linkedin_review,
    ))
