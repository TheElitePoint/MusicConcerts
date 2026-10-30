"""Discover new SOP-role LinkedIn people at the strongest qualified companies.

This is a URL/name discovery stage only. Every admitted row remains unverified
until a separate full-profile pass confirms the current employer and title.
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

from mp.db import utcnow  # noqa: E402
from mp import config  # noqa: E402
from mp.normalize import linkedin_person_url, normalize_company_name, normalize_person_name  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402


ACTOR = "harvestapi~linkedin-company-employees"
BASE = "https://api.apify.com/v2"


def _slug(value: str) -> str:
    match = re.search(r"linkedin\.com/company/([^/?#]+)", value or "", re.I)
    return match.group(1).strip().lower() if match else ""


def _company_key(value: str) -> str:
    return normalize_company_name(value or "")


def _position(item: dict[str, Any]) -> tuple[str, str, str]:
    entries = item.get("currentPosition") or item.get("currentPositions") or []
    if isinstance(entries, dict):
        entries = [entries]
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            company = str(entry.get("companyName") or entry.get("company") or "").strip()
            title = str(entry.get("position") or entry.get("title") or "").strip()
            company_url = str(entry.get("companyLinkedinUrl") or entry.get("companyUrl") or "").strip()
            if company and title:
                return company, title, company_url
    return (
        str(item.get("company") or item.get("currentCompany") or "").strip(),
        str(item.get("job_title") or item.get("title") or item.get("headline") or "").strip(),
        str(item.get("companyLinkedinUrl") or "").strip(),
    )


def _person(item: dict[str, Any]) -> tuple[str, str]:
    name = str(item.get("full_name") or item.get("fullName") or item.get("name") or "").strip()
    if not name:
        name = " ".join(
            part for part in (
                str(item.get("first_name") or item.get("firstName") or "").strip(),
                str(item.get("last_name") or item.get("lastName") or "").strip(),
            ) if part
        )
    url = linkedin_person_url(str(
        item.get("linkedin_url") or item.get("linkedinUrl")
        or item.get("profileUrl") or item.get("url") or ""
    ))
    return name, url


def _targets(con: sqlite3.Connection, limit: int, offset: int = 0,
             company_ids: set[int] | None = None) -> list[sqlite3.Row]:
    rows = con.execute(
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
              AND (SELECT COUNT(*) FROM people p JOIN profiles pr USING(person_id)
                    WHERE p.company_id=c.company_id AND p.status!='REJECTED'
                      AND pr.channel='linkedin') < 10
            ORDER BY max_score DESC,active_linkedin ASC,c.company_id
            LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()
    if company_ids:
        rows = [row for row in rows if int(row["company_id"]) in company_ids]
    return rows


def _start(token: str, companies: list[str], per_company: int) -> str:
    response = requests.post(
        f"{BASE}/acts/{ACTOR}/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "profileScraperMode": "Short ($4 per 1k)",
            "companies": companies,
            "companyBatchMode": "one_by_one",
            "maxItemsPerCompany": per_company,
            "maxItems": per_company * len(companies),
            "takePages": 1,
        },
        timeout=60,
    )
    response.raise_for_status()
    return str((response.json().get("data") or {}).get("id") or "")


def _wait(token: str, run_id: str, max_minutes: int) -> dict[str, Any]:
    import time
    started = time.monotonic()
    while time.monotonic() - started < max_minutes * 60:
        response = requests.get(
            f"{BASE}/actor-runs/{run_id}",
            headers={"Authorization": f"Bearer {token}"}, timeout=30,
        )
        response.raise_for_status()
        run = response.json().get("data") or {}
        status = str(run.get("status") or "")
        print(f"discovery_run status={status}", flush=True)
        if status in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}:
            return run
        time.sleep(20)
    raise TimeoutError("company employee discovery timed out")


def main(limit: int, per_company: int, max_minutes: int, offset: int = 0,
         account_index: int = 0, company_ids: set[int] | None = None) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token and account_index > 0:
        tokens = config.keys("apify")
        if account_index > len(tokens):
            raise SystemExit(f"Configured Apify account {account_index} does not exist")
        token = tokens[account_index - 1]
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    try:
        targets = _targets(con, limit, offset, company_ids)
        print(f"targets={len(targets)}", flush=True)
        if not targets:
            return 0
        run_id = _start(token, [row["linkedin_url"] for row in targets], per_company)
        run = _wait(token, run_id, max_minutes)
        if run.get("status") != "SUCCEEDED":
            raise RuntimeError(f"discovery actor ended with {run.get('status')}")
        dataset_id = str(run.get("defaultDatasetId") or "")
        response = requests.get(
            f"{BASE}/datasets/{dataset_id}/items",
            headers={"Authorization": f"Bearer {token}"},
            params={"format": "json", "clean": "true"}, timeout=180,
        )
        response.raise_for_status()
        items = response.json()
        if not isinstance(items, list):
            items = []
        folder = ROOT / "data/acquisition" / (
            "fresh_company_employees_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )
        folder.mkdir(parents=True, exist_ok=True)
        raw_path = folder / f"run_{run_id}.json"
        raw_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

        by_slug = {_slug(row["linkedin_url"]): row for row in targets}
        by_name: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in targets:
            by_name[_company_key(row["canonical_name"])].append(row)
        seen = {str(row[0]).lower() for row in con.execute(
            "SELECT url FROM profiles WHERE channel='linkedin'"
        )}
        inserted = duplicate = employer_reject = role_reject = malformed = 0
        per_target: defaultdict[int, int] = defaultdict(int)
        for item in items:
            if not isinstance(item, dict):
                continue
            company, title, company_url = _position(item)
            target = by_slug.get(_slug(company_url))
            if target is None:
                matches = by_name.get(_company_key(company), [])
                target = matches[0] if len(matches) == 1 else None
            if target is None:
                employer_reject += 1
                continue
            if _company_key(company) != _company_key(target["canonical_name"]):
                employer_reject += 1
                continue
            name, url = _person(item)
            if not name or not url:
                malformed += 1
                continue
            if url.lower() in seen:
                duplicate += 1
                continue
            role = classify_title(title, small_company=(target["roster_estimate"] or 99) <= 20)
            if not role.qualifies:
                role_reject += 1
                continue
            capacity = max(0, 25 - int(target["active_linkedin"] or 0))
            if per_target[int(target["company_id"])] >= capacity:
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
                seen.add(url.lower())
                continue
            con.execute(
                """INSERT INTO person_evidence
                   (person_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(person_id,kind,url) DO UPDATE SET
                     source=excluded.source,snippet=excluded.snippet,
                     strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                (pid, "employment", url, "apify_company_employee_discovery_fresh",
                 f"Company employee discovery result: {title} at {company}", "strong", utcnow()),
            )
            per_target[int(target["company_id"])] += 1
            seen.add(url.lower())
            inserted += 1
        con.commit()
        print(
            f"items={len(items)} inserted={inserted} duplicates={duplicate} "
            f"employer_reject={employer_reject} role_reject={role_reject} malformed={malformed} raw={raw_path}",
            flush=True,
        )
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--per-company", type=int, default=25)
    parser.add_argument("--max-minutes", type=int, default=30)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--account-index", type=int, default=0)
    parser.add_argument("--company-ids", default="")
    args = parser.parse_args()
    company_ids = {int(value) for value in args.company_ids.split(",") if value.strip()}
    raise SystemExit(main(max(1, args.limit), max(1, args.per_company), max(5, args.max_minutes), max(0, args.offset), max(0, args.account_index), company_ids))
