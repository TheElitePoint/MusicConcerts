"""Discover novel LinkedIn decision-makers with company-scoped Google SERPs.

The source companies have already passed every company hard gate.  This stage
searches only those companies, requires a visible company-name and SOP-role
match in the Google result, normalizes LinkedIn URLs for cross-country dedupe,
and stores only weak discovery evidence.  A later profile-scraper pass must
confirm the current employer and title before anyone can receive FINAL PASS.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from expand_salesnav_batched import _human_name  # noqa: E402
from mp.db import utcnow  # noqa: E402
from mp.normalize import (  # noqa: E402
    _fold,
    linkedin_person_url,
    loose_company_key,
    normalize_company_name,
    normalize_person_name,
)
from mp.sop.roles import classify_title  # noqa: E402


ACTOR_URL = (
    "https://api.apify.com/v2/acts/apify~google-search-scraper/"
    "run-sync-get-dataset-items?format=json&clean=true&timeout=300"
)
ROLE_QUERIES = (
    '(founder OR owner OR president OR CEO OR "managing director" OR director)',
    '("A&R" OR "artist development" OR "label manager" OR marketing OR partnerships OR operations)',
)
TITLE_SPLIT_RE = re.compile(r"\s+(?:-|–|—|\|)\s+")


def _targets(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT c.company_id,c.canonical_name,c.roster_estimate,c.website
             FROM companies c
            WHERE c.status='QUALIFIED'
              AND (SELECT COUNT(*) FROM people p
                     JOIN scores s ON s.person_id=p.person_id AND s.channel='linkedin'
                    WHERE p.company_id=c.company_id AND p.status!='REJECTED'
                      AND s.verdict='FINAL PASS') < 10
            ORDER BY c.company_id"""
    ).fetchall()


def _existing_urls(con: sqlite3.Connection) -> set[str]:
    out: set[str] = set()
    for row in con.execute("SELECT url FROM profiles WHERE channel='linkedin'"):
        url = linkedin_person_url(row[0] or "")
        if url:
            out.add(url.lower())
    # The 127-row separately delivered cohort is not guaranteed to remain in a
    # historical run CSV, so include its explicit delivery artifact as well.
    try:
        rows = json.loads((ROOT / "data/net_new_linkedin_final.json").read_text(encoding="utf-8")).get("rows") or []
    except (OSError, ValueError):
        rows = []
    for row in rows:
        url = linkedin_person_url(str(row.get("Person LinkedIn URL") or ""))
        if url:
            out.add(url.lower())
    return out


def _query(company: str, role_clause: str) -> str:
    # Exact company phrase is intentionally repeated in every query.  It is the
    # first identity guard; current-employer verification remains mandatory.
    safe = company.replace('"', " ").strip()
    return f'site:linkedin.com/in/ "{safe}" {role_clause}'


def _search(token: str, queries: list[str]) -> list[dict[str, Any]]:
    payload = {
        "queries": "\n".join(queries),
        "maxPagesPerQuery": 1,
        "mobileResults": False,
        "includeUnfilteredResults": False,
        "saveHtml": False,
        "saveHtmlToKeyValueStore": False,
        "includeIcons": False,
    }
    response = requests.post(
        ACTOR_URL,
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=360,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _dataset(token: str, dataset_id: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items",
        headers={"Authorization": f"Bearer {token}"},
        params={"format": "json", "clean": "true"},
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _save(batch: int, items: list[dict[str, Any]]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "data/acquisition" / f"google_linkedin_people_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"batch_{batch:02d}.json"
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _term(item: dict[str, Any]) -> str:
    search = item.get("searchQuery") or {}
    return str(search.get("term") or search.get("query") or item.get("query") or "")


def _completed_terms() -> set[str]:
    out: set[str] = set()
    for path in ROOT.glob("data/acquisition/google_linkedin_people_*/batch_*.json"):
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(items, list):
            continue
        out.update(_term(item) for item in items if isinstance(item, dict) and _term(item))
    return out


def _organic(item: dict[str, Any]) -> list[dict[str, Any]]:
    values = item.get("organicResults") or item.get("organic_results") or []
    return values if isinstance(values, list) else []


def _parse_hit(hit: dict[str, Any]) -> tuple[str, str, str, str]:
    url = linkedin_person_url(str(hit.get("url") or hit.get("link") or ""))
    title = str(hit.get("title") or "").strip()
    snippet = str(hit.get("description") or hit.get("snippet") or hit.get("text") or "").strip()
    parts = TITLE_SPLIT_RE.split(title, maxsplit=1)
    name = parts[0].strip()
    role_text = parts[1].strip() if len(parts) > 1 else ""
    # Google sometimes renders "Name's Post" for a person URL.  It is not an
    # identity/title result and must not be interpreted as a person name.
    if name.lower().endswith(("'s post", "’s post", " posts")):
        name = ""
    return name, role_text, url, snippet


def _company_visible(company: str, corpus: str) -> bool:
    strict = normalize_company_name(company)
    folded = _fold(corpus)
    if strict and strict in folded:
        return True
    loose = loose_company_key(company)
    tokens = [token for token in loose.split() if len(token) >= 3]
    # Loose matching is only a discovery convenience.  Requiring all remaining
    # distinctive tokens avoids admitting a result on one generic word.
    return len(tokens) >= 2 and all(re.search(rf"(?<!\w){re.escape(t)}(?!\w)", folded) for t in tokens)


def _admit(
    con: sqlite3.Connection,
    items: list[dict[str, Any]],
    query_map: dict[str, sqlite3.Row],
    seen: set[str],
) -> dict[str, int]:
    stats: defaultdict[str, int] = defaultdict(int)
    per_company: defaultdict[int, int] = defaultdict(int)
    for item in items:
        target = query_map.get(_term(item))
        if target is None:
            stats["unmapped_pages"] += 1
            continue
        for hit in _organic(item):
            stats["organic"] += 1
            name, role_text, url, snippet = _parse_hit(hit)
            if not url or not _human_name(name):
                stats["identity_rejects"] += 1
                continue
            url_key = url.lower()
            if url_key in seen:
                stats["duplicates"] += 1
                continue
            corpus = " ".join(part for part in (role_text, snippet) if part)
            if not _company_visible(target["canonical_name"], corpus):
                stats["company_rejects"] += 1
                continue
            small = (target["roster_estimate"] or 99) <= 20
            role = classify_title(corpus, small_company=small)
            if not role.qualifies:
                stats["role_rejects"] += 1
                continue
            # Keep enough candidates to survive verification failures while
            # bounding same-company noise.  The final-sheet cap remains 10.
            if per_company[target["company_id"]] >= 25:
                stats["candidate_cap_rejects"] += 1
                continue
            norm = normalize_person_name(name)
            con.execute(
                """INSERT INTO people
                   (company_id,full_name,name_normalized,title,title_family,seniority,
                    role_relevance,status,first_seen)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(company_id,name_normalized) DO UPDATE SET
                     title=CASE WHEN people.current_verified=0 THEN excluded.title ELSE people.title END,
                     title_family=CASE WHEN people.current_verified=0 THEN excluded.title_family ELSE people.title_family END,
                     seniority=CASE WHEN people.current_verified=0 THEN excluded.seniority ELSE people.seniority END,
                     role_relevance=CASE WHEN people.current_verified=0 THEN excluded.role_relevance ELSE people.role_relevance END""",
                (
                    target["company_id"], name, norm, role_text or corpus[:240],
                    role.family, role.seniority, role.relevance, "CANDIDATE", utcnow(),
                ),
            )
            person = con.execute(
                "SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
                (target["company_id"], norm),
            ).fetchone()
            if not person:
                continue
            pid = int(person[0])
            inserted = con.execute(
                """INSERT OR IGNORE INTO profiles
                   (person_id,channel,url,handle,verification_status,retrieved_at)
                   VALUES (?,?,?,?,?,?)""",
                (pid, "linkedin", url, url.rstrip("/").rsplit("/", 1)[-1], "UNVERIFIED", utcnow()),
            )
            if not inserted.rowcount:
                stats["duplicates"] += 1
                seen.add(url_key)
                continue
            evidence = f"Google LinkedIn result: {role_text}; {snippet}".strip("; ")
            con.execute(
                """INSERT INTO person_evidence
                   (person_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(person_id,kind,url) DO UPDATE SET
                     source=excluded.source,snippet=excluded.snippet,
                     strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                (pid, "employment", url, "apify_google_serp", evidence[:1500], "weak", utcnow()),
            )
            seen.add(url_key)
            per_company[target["company_id"]] += 1
            stats["inserted"] += 1
    con.commit()
    return dict(stats)


def main(batch_size: int, query_families: int, recover_dataset_id: str = "") -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    try:
        targets = _targets(con)
        seen = _existing_urls(con)
        jobs: list[tuple[str, sqlite3.Row]] = []
        for target in targets:
            for clause in ROLE_QUERIES[:query_families]:
                jobs.append((_query(target["canonical_name"], clause), target))
        print(f"targets={len(targets)} queries={len(jobs)} existing_urls={len(seen)}", flush=True)
        totals: defaultdict[str, int] = defaultdict(int)
        completed_terms = _completed_terms()
        if completed_terms:
            jobs = [(query, target) for query, target in jobs if query not in completed_terms]
            print(f"resume_completed_terms={len(completed_terms)} remaining_queries={len(jobs)}", flush=True)
        if recover_dataset_id:
            recovered = _dataset(token, recover_dataset_id)
            all_query_map = {query: target for query, target in jobs}
            path = _save(0, recovered)
            stats = _admit(con, recovered, all_query_map, seen)
            recovered_terms = {_term(item) for item in recovered if _term(item)}
            completed_terms.update(recovered_terms)
            for key, value in stats.items():
                totals[key] += value
            print(
                f"recovered_pages={len(recovered)} completed_terms={len(recovered_terms)} "
                f"inserted={stats.get('inserted', 0)} raw={path}",
                flush=True,
            )
            jobs = [(query, target) for query, target in jobs if query not in recovered_terms]
            print(f"remaining_queries={len(jobs)}", flush=True)
        for offset in range(0, len(jobs), batch_size):
            batch = jobs[offset : offset + batch_size]
            query_map = {query: target for query, target in batch}
            items = _search(token, list(query_map))
            path = _save(offset // batch_size + 1, items)
            stats = _admit(con, items, query_map, seen)
            for key, value in stats.items():
                totals[key] += value
            print(
                f"batch={offset // batch_size + 1} pages={len(items)} "
                f"inserted={stats.get('inserted', 0)} raw={path}",
                flush=True,
            )
        print(" ".join(f"{k}={v}" for k, v in sorted(totals.items())), flush=True)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--query-families", type=int, choices=(1, 2), default=2)
    parser.add_argument("--recover-dataset-id", default="")
    args = parser.parse_args()
    raise SystemExit(main(max(1, min(args.batch_size, 100)), args.query_families, args.recover_dataset_id.strip()))
