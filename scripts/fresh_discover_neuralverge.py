"""Run one URL-scoped NeuralVerge employee search per strong qualified company."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from mp.db import utcnow  # noqa: E402
from mp.normalize import linkedin_person_url, normalize_person_name  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402
from verify_profiles_apimaestro import _employer_matches  # noqa: E402


BASE = "https://api.apify.com/v2"
ACTOR = "neuralverge~neuralverge-linkedin-company-employee"


def _targets(con: sqlite3.Connection, limit: int, offset: int = 0) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT c.company_id,c.canonical_name,c.linkedin_url,c.roster_estimate,
                  (SELECT MAX(s.total) FROM people p JOIN scores s USING(person_id)
                    WHERE p.company_id=c.company_id AND s.channel='linkedin'
                      AND s.verdict='FINAL PASS') AS max_score,
                  (SELECT COUNT(*) FROM people p JOIN profiles pr USING(person_id)
                    WHERE p.company_id=c.company_id AND p.status!='REJECTED'
                      AND pr.channel='linkedin') AS active_linkedin
             FROM companies c
            WHERE c.status='QUALIFIED'
              AND c.linkedin_url LIKE '%linkedin.com/company/%'
              AND EXISTS (SELECT 1 FROM people p JOIN scores s USING(person_id)
                           WHERE p.company_id=c.company_id AND s.channel='linkedin'
                             AND s.verdict='FINAL PASS' AND s.total>=90)
              AND (SELECT COUNT(*) FROM people p JOIN profiles pr USING(person_id)
                    WHERE p.company_id=c.company_id AND p.status!='REJECTED'
                      AND pr.channel='linkedin') < 10
            ORDER BY max_score DESC,active_linkedin ASC,c.company_id
            LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()


def _one(token: str, row: dict[str, Any], max_results: int, max_minutes: int) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    response = requests.post(
        f"{BASE}/acts/{ACTOR}/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={"companies": [row["linkedin_url"]], "maxResults": max_results},
        timeout=60,
    )
    response.raise_for_status()
    run_id = str((response.json().get("data") or {}).get("id") or "")
    if not run_id:
        raise RuntimeError("actor returned no run id")
    started = time.monotonic()
    run: dict[str, Any] = {}
    while time.monotonic() - started < max_minutes * 60:
        status_response = requests.get(
            f"{BASE}/actor-runs/{run_id}",
            headers={"Authorization": f"Bearer {token}"}, timeout=30,
        )
        status_response.raise_for_status()
        run = status_response.json().get("data") or {}
        if run.get("status") in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}:
            break
        time.sleep(15)
    if run.get("status") != "SUCCEEDED":
        raise RuntimeError(f"actor ended with {run.get('status')}")
    dataset_id = str(run.get("defaultDatasetId") or "")
    items_response = requests.get(
        f"{BASE}/datasets/{dataset_id}/items",
        headers={"Authorization": f"Bearer {token}"},
        params={"format": "json", "clean": "true"}, timeout=180,
    )
    items_response.raise_for_status()
    items = items_response.json()
    return row, run_id, items if isinstance(items, list) else []


def main(limit: int, max_results: int, workers: int, max_minutes: int, offset: int = 0) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    try:
        targets = [dict(row) for row in _targets(con, limit, offset)]
        print(f"targets={len(targets)}", flush=True)
        folder = ROOT / "data/acquisition" / (
            "fresh_neuralverge_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )
        folder.mkdir(parents=True, exist_ok=True)
        results: list[tuple[dict[str, Any], str, list[dict[str, Any]]]] = []
        failed = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_one, token, row, max_results, max_minutes): row
                for row in targets
            }
            for future in as_completed(futures):
                row = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                    print(f"fetched company={row['canonical_name']} items={len(result[2])}", flush=True)
                except Exception as exc:
                    failed += 1
                    print(f"failed company={row['canonical_name']} error={type(exc).__name__}", flush=True)

        seen = {str(row[0]).lower() for row in con.execute(
            "SELECT url FROM profiles WHERE channel='linkedin'"
        )}
        inserted = duplicate = employer_reject = role_reject = malformed = 0
        for target, run_id, items in results:
            (folder / f"run_{run_id}.json").write_text(
                json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("fullName") or " ".join(
                    value for value in (
                        str(item.get("firstName") or "").strip(),
                        str(item.get("lastName") or "").strip(),
                    ) if value
                )).strip()
                title = str(item.get("title") or "").strip()
                company = str(item.get("company") or target["canonical_name"]).strip()
                url = linkedin_person_url(str(item.get("linkedinUrl") or ""))
                if not (name and title and url):
                    malformed += 1
                    continue
                if not _employer_matches(target["canonical_name"], company):
                    employer_reject += 1
                    continue
                if url.lower() in seen:
                    duplicate += 1
                    continue
                role = classify_title(title, small_company=(target["roster_estimate"] or 99) <= 20)
                if not role.qualifies:
                    role_reject += 1
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
                    (target["company_id"], name, norm, title, role.family, role.seniority,
                     role.relevance, "CANDIDATE", utcnow()),
                )
                person = con.execute(
                    "SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
                    (target["company_id"], norm),
                ).fetchone()
                if not person:
                    continue
                pid = int(person[0])
                cur = con.execute(
                    """INSERT OR IGNORE INTO profiles
                       (person_id,channel,url,handle,verification_status,retrieved_at)
                       VALUES (?,?,?,?,?,?)""",
                    (pid, "linkedin", url, url.rstrip("/").rsplit("/", 1)[-1], "UNVERIFIED", utcnow()),
                )
                if not cur.rowcount:
                    duplicate += 1
                    continue
                con.execute(
                    """INSERT INTO person_evidence
                       (person_id,kind,url,source,snippet,strength,retrieved_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(person_id,kind,url) DO UPDATE SET
                         source=excluded.source,snippet=excluded.snippet,
                         strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                    (pid, "employment", url, "apify_company_employee_discovery_fresh",
                     f"URL-scoped company employee result: {title} at {company}", "strong", utcnow()),
                )
                seen.add(url.lower())
                inserted += 1
        con.commit()
        print(
            f"runs={len(results)} failed={failed} inserted={inserted} duplicates={duplicate} "
            f"employer_reject={employer_reject} role_reject={role_reject} malformed={malformed} raw={folder}",
            flush=True,
        )
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--max-results", type=int, default=25)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--max-minutes", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    raise SystemExit(main(max(1, args.limit), max(1, args.max_results), max(1, min(args.workers, 5)), max(5, args.max_minutes), max(0, args.offset)))
