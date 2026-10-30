"""Verify Google-discovered LinkedIn candidates against current profile data.

This is the SS34/SS42 hard-gate upgrade: search-result snippets stay weak until
the supplied LinkedIn URL returns a current experience whose employer matches
the already-qualified company and whose title still qualifies under SS30/SS31.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import sqlite3
import sys
import time
from urllib.parse import unquote
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.normalize import linkedin_slug, loose_company_key, normalize_company_name  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402


ACTOR = "apimaestro~linkedin-profile-batch-scraper-no-cookies-required"
BASE = "https://api.apify.com/v2"


def _candidates(con: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    sql = """SELECT p.person_id,p.company_id,p.full_name,p.role_relevance,
                    c.canonical_name AS company,c.roster_estimate,pr.url
               FROM people p
               JOIN companies c ON c.company_id=p.company_id
               JOIN profiles pr ON pr.person_id=p.person_id AND pr.channel='linkedin'
              WHERE c.status='QUALIFIED' AND p.status!='REJECTED'
                AND p.current_verified=0
                AND EXISTS (SELECT 1 FROM person_evidence e
                             WHERE e.person_id=p.person_id
                               AND e.source IN ('apify_google_serp',
                                                'apify_company_employee_discovery_fresh',
                                                'thirdwatch_candidate_finder'))
              ORDER BY p.role_relevance DESC,
                       CASE p.seniority WHEN 'executive' THEN 0 WHEN 'senior' THEN 1 ELSE 2 END,
                       p.person_id"""
    if limit > 0:
        sql += " LIMIT ?"
        return con.execute(sql, (limit,)).fetchall()
    return con.execute(sql).fetchall()


def _start(token: str, usernames: list[str]) -> str:
    response = requests.post(
        f"{BASE}/acts/{ACTOR}/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={"usernames": usernames, "includeEmail": False},
        timeout=60,
    )
    response.raise_for_status()
    return str((response.json().get("data") or {}).get("id") or "")


def _wait(token: str, run_id: str, max_minutes: int, *, verbose: bool = True) -> dict[str, Any]:
    started = time.monotonic()
    while time.monotonic() - started < max_minutes * 60:
        response = requests.get(
            f"{BASE}/actor-runs/{run_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        status = str(data.get("status") or "")
        elapsed = int(time.monotonic() - started)
        if verbose:
            print(f"verification_run status={status} elapsed={elapsed}s", flush=True)
        if status in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}:
            return data
        time.sleep(20)
    raise TimeoutError(f"Verification actor did not finish within {max_minutes} minutes")


def _items(token: str, dataset_id: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"{BASE}/datasets/{dataset_id}/items",
        headers={"Authorization": f"Bearer {token}"},
        params={"format": "json", "clean": "true"},
        timeout=180,
    )
    response.raise_for_status()
    value = response.json()
    return value if isinstance(value, list) else []


def _save(run_id: str, items: list[dict[str, Any]]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "data/acquisition" / f"apimaestro_profile_verification_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"run_{run_id}.json"
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _flatten(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        results = item.get("results")
        if isinstance(results, dict):
            for username, profile in results.items():
                if isinstance(profile, dict):
                    out[str(username).lower()] = profile
            continue
        basic = item.get("basic_info") or item.get("basicInfo") or {}
        username = str(
            item.get("username") or item.get("publicIdentifier")
            or item.get("public_identifier") or item.get("profileUrl")
            or (basic.get("public_identifier") if isinstance(basic, dict) else "")
            or ""
        ).strip().lower()
        if username:
            out[username] = item
    return out


def _current_experience(profile: dict[str, Any], expected_company: str) -> tuple[str, str]:
    experiences = (
        profile.get("experience") or profile.get("experiences")
        or profile.get("positions") or profile.get("currentPositions")
        or profile.get("workExperience") or []
    )
    if isinstance(experiences, dict):
        experiences = list(experiences.values())
    if not isinstance(experiences, list):
        experiences = []
    candidates: list[tuple[bool, str, str]] = []
    for exp in experiences:
        if not isinstance(exp, dict):
            continue
        title = str(
            exp.get("title") or exp.get("position") or exp.get("jobTitle")
            or exp.get("job_title") or exp.get("raw_job_title") or ""
        ).strip()
        company_value = (
            exp.get("company") or exp.get("companyName") or exp.get("company_name")
            or exp.get("raw_company_name") or exp.get("organization") or ""
        )
        if isinstance(company_value, dict):
            company = str(company_value.get("name") or company_value.get("companyName") or "").strip()
        else:
            company = str(company_value).strip()
        duration = " ".join(
            str(exp.get(key) or "") for key in ("duration", "dateRange", "endDate", "end")
        ).lower()
        current = bool(
            exp.get("isCurrent") or exp.get("current") or exp.get("is_current")
            or exp.get("job_still_working")
        ) or "present" in duration
        if title and company:
            candidates.append((current, title, company))
    if not candidates:
        basic = profile.get("basic_info") or profile.get("basicInfo") or profile
        if isinstance(basic, dict):
            title = str(
                basic.get("title") or basic.get("headline") or basic.get("job_title")
                or basic.get("current_job_title") or ""
            ).strip()
            company = str(
                basic.get("company") or basic.get("currentCompany")
                or basic.get("current_company_name") or basic.get("company_name") or ""
            ).strip()
            return company, title
        return "", ""
    current_rows = [row for row in candidates if row[0]]
    matching_current = [row for row in current_rows if _employer_matches(expected_company, row[2])]
    # A person may hold several current positions.  The SOP requires that the
    # target-company role be current, not that it be LinkedIn's first/top card.
    chosen = (matching_current or current_rows or candidates)[0]
    return chosen[2], chosen[1]


def _employer_matches(expected: str, found: str) -> bool:
    a = normalize_company_name(expected)
    b = normalize_company_name(found)
    if not (a and b):
        return False
    if a == b:
        return True
    # Permit harmless industry-descriptor variants ("Soundway" vs
    # "Soundway Records"; "Frontiers Music" vs "Frontiers Label Group") but
    # never generic substring collisions ("Noise Records" vs "White Noise
    # Records", "FLG" vs "FLG Partners", or "4AD" vs "4AD Enterprises").
    def loose_tokens(value: str) -> tuple[str, ...]:
        out: list[str] = []
        for token in loose_company_key(value).split():
            if token not in out:
                out.append(token)
        return tuple(out)
    return bool(loose_tokens(expected) and loose_tokens(expected) == loose_tokens(found))


def _apply(con: sqlite3.Connection, rows: list[sqlite3.Row], profiles: dict[str, dict[str, Any]]) -> dict[str, int]:
    stats = {"confirmed": 0, "employer_mismatch": 0, "role_reject": 0, "missing": 0}
    now = utcnow()
    for row in rows:
        _kind, slug = linkedin_slug(row["url"])
        profile = profiles.get(unquote(slug).lower()) if slug else None
        if not profile:
            stats["missing"] += 1
            continue
        company, title = _current_experience(profile, row["company"])
        if not _employer_matches(row["company"], company):
            con.execute(
                "UPDATE people SET status='REJECTED',reject_reason=?,last_researched=? WHERE person_id=?",
                (f"SS34 employer mismatch: LinkedIn shows '{company or 'unknown'}', not '{row['company']}'", now, row["person_id"]),
            )
            con.execute(
                "UPDATE profiles SET verification_status='REJECTED',verified_at=? WHERE person_id=? AND channel='linkedin'",
                (now, row["person_id"]),
            )
            stats["employer_mismatch"] += 1
            continue
        role = classify_title(title, small_company=(row["roster_estimate"] or 99) <= 20)
        if not role.qualifies:
            con.execute(
                "UPDATE people SET status='REJECTED',reject_reason=?,last_researched=? WHERE person_id=?",
                (role.exclusion_reason or f"SS30 role mismatch after profile verification: '{title}'", now, row["person_id"]),
            )
            con.execute(
                "UPDATE profiles SET verification_status='REJECTED',verified_at=? WHERE person_id=? AND channel='linkedin'",
                (now, row["person_id"]),
            )
            stats["role_reject"] += 1
            continue
        con.execute(
            """UPDATE people SET title=?,title_family=?,seniority=?,role_relevance=?,
                      current_verified=1,employment_sources=1,status='VERIFIED',
                      reject_reason=NULL,last_researched=? WHERE person_id=?""",
            (title, role.family, role.seniority, role.relevance, now, row["person_id"]),
        )
        con.execute(
            """UPDATE profiles SET verification_status='VERIFIED',usability='full',
                      verified_at=?,retrieved_at=? WHERE person_id=? AND channel='linkedin'""",
            (now, now, row["person_id"]),
        )
        con.execute(
            """INSERT INTO person_evidence
               (person_id,kind,url,source,snippet,strength,retrieved_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(person_id,kind,url) DO UPDATE SET
                 source=excluded.source,snippet=excluded.snippet,
                 strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
            (
                row["person_id"], "employment", row["url"], "apify_linkedin_apimaestro",
                f"LinkedIn current position: {title} at {company}", "strongest", now,
            ),
        )
        stats["confirmed"] += 1
    con.commit()
    return stats


def main(limit: int, max_minutes: int, chunk_size: int, workers: int) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    try:
        rows = _candidates(con, limit)
        usernames = []
        for row in rows:
            _kind, slug = linkedin_slug(row["url"])
            if slug:
                usernames.append(slug)
        print(f"verification_candidates={len(rows)} usernames={len(usernames)}", flush=True)
        if not usernames:
            return 0
        chunks = [usernames[i:i + chunk_size] for i in range(0, len(usernames), chunk_size)]
        print(f"verification_chunks={len(chunks)} chunk_size={chunk_size} workers={workers}", flush=True)

        def run_chunk(number: int, values: list[str]) -> tuple[int, str, list[dict[str, Any]], Path]:
            run_id = _start(token, values)
            if not run_id:
                raise RuntimeError("Apify did not return a run ID")
            run = _wait(token, run_id, max_minutes, verbose=False)
            if run.get("status") != "SUCCEEDED":
                raise RuntimeError(f"Verification actor ended with {run.get('status')}")
            items = _items(token, str(run.get("defaultDatasetId") or ""))
            return number, run_id, items, _save(run_id, items)

        profiles: dict[str, dict[str, Any]] = {}
        dataset_items = 0
        raw_paths: list[Path] = []
        failed_chunks = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(run_chunk, number, values): number
                for number, values in enumerate(chunks, 1)
            }
            completed = 0
            for future in as_completed(futures):
                number = futures[future]
                try:
                    _number, run_id, items, path = future.result()
                except Exception as exc:  # keep other paid chunks usable
                    failed_chunks += 1
                    print(f"chunk={number} failed={type(exc).__name__}", flush=True)
                    continue
                dataset_items += len(items)
                profiles.update(_flatten(items))
                raw_paths.append(path)
                completed += 1
                if completed % 5 == 0 or completed == len(chunks):
                    print(
                        f"verification_progress completed={completed}/{len(chunks)} "
                        f"profiles={len(profiles)} failed_chunks={failed_chunks}",
                        flush=True,
                    )
        stats = _apply(con, rows, profiles)
        print(
            f"dataset_items={dataset_items} profiles={len(profiles)} "
            f"raw_runs={len(raw_paths)} failed_chunks={failed_chunks}",
            flush=True,
        )
        print(" ".join(f"{key}={value}" for key, value in stats.items()), flush=True)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="0 verifies every eligible candidate")
    parser.add_argument("--max-minutes", type=int, default=45)
    parser.add_argument("--chunk-size", type=int, default=8)
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args()
    raise SystemExit(main(
        max(0, args.limit), max(5, args.max_minutes),
        max(1, min(args.chunk_size, 8)), max(1, min(args.workers, 5)),
    ))
