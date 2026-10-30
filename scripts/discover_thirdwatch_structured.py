"""Run one structured current-company discovery per qualified company.

Results are stored as unverified candidates. A later Data Slayer batch must
confirm current employer and title before scoring can produce FINAL PASS.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from expand_salesnav_batched import _budget_left  # noqa: E402
from expand_thirdwatch_verified import CANDIDATE_ACTOR_ID  # noqa: E402
from ingest_thirdwatch_candidates import main as ingest  # noqa: E402

BASE = "https://api.apify.com/v2"


def _searched() -> set[int]:
    out: set[int] = set()
    for path in ROOT.glob("data/acquisition/thirdwatch_structured_discovery_*/company_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (payload.get("input") or {}).get("jobTitles"):
            continue
        match = re.search(r"company_(\d+)\.json$", path.name)
        if match:
            out.add(int(match.group(1)))
    return out


def _targets(con: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    searched = _searched()
    rows = con.execute(
        """SELECT c.company_id,c.canonical_name,c.roster_estimate,
                  (SELECT COUNT(*) FROM people p JOIN scores s ON s.person_id=p.person_id
                    WHERE p.company_id=c.company_id AND p.status!='REJECTED'
                      AND s.channel='linkedin' AND s.verdict='FINAL PASS') final_people,
                  (SELECT MAX(s.total) FROM people p JOIN scores s ON s.person_id=p.person_id
                    WHERE p.company_id=c.company_id AND p.status!='REJECTED'
                      AND s.channel='linkedin') max_score,
                  (SELECT COUNT(DISTINCT a.signal_type) FROM activity_signals a
                    WHERE a.company_id=c.company_id AND a.observed_at IS NOT NULL) activity_categories
             FROM companies c
            WHERE c.status='QUALIFIED'
              AND (SELECT COUNT(*) FROM people p JOIN profiles pr
                    ON pr.person_id=p.person_id AND pr.channel='linkedin'
                    WHERE p.company_id=c.company_id AND p.status!='REJECTED') < 10
            ORDER BY (final_people>0) DESC, COALESCE(max_score,0) DESC,
                     activity_categories DESC, c.company_id"""
    ).fetchall()
    return [row for row in rows if int(row["company_id"]) not in searched][:limit]


def _run(token: str, payload: dict, max_minutes: int) -> list[dict]:
    response = requests.post(
        f"{BASE}/acts/{CANDIDATE_ACTOR_ID}/runs",
        headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=60,
    )
    response.raise_for_status()
    run_id = str((response.json().get("data") or {}).get("id") or "")
    if not run_id:
        raise RuntimeError("Apify did not return a run ID")
    started = time.monotonic()
    run: dict = {}
    while time.monotonic() - started < max_minutes * 60:
        status_response = requests.get(
            f"{BASE}/actor-runs/{run_id}",
            headers={"Authorization": f"Bearer {token}"}, timeout=30,
        )
        status_response.raise_for_status()
        run = status_response.json().get("data") or {}
        if run.get("status") in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}:
            break
        time.sleep(10)
    if run.get("status") != "SUCCEEDED":
        raise RuntimeError(f"candidate actor ended with {run.get('status')}")
    dataset_id = str(run.get("defaultDatasetId") or "")
    result = requests.get(
        f"{BASE}/datasets/{dataset_id}/items",
        headers={"Authorization": f"Bearer {token}"},
        params={"format": "json", "clean": "true"}, timeout=120,
    )
    result.raise_for_status()
    body = result.json()
    return [item for item in body if isinstance(item, dict)] if isinstance(body, list) else []


def main(company_limit: int, per_company: int, max_minutes: int) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "data/acquisition" / f"thirdwatch_structured_discovery_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    total = 0
    try:
        targets = _targets(con, company_limit)
        print(f"targets={len(targets)} remaining=${_budget_left(token):.2f}", flush=True)
        for index, target in enumerate(targets, 1):
            if _budget_left(token) < 0.80:
                print("stopping: reserved $0.80 for verification", flush=True)
                break
            payload = {
                "currentCompanies": [target["canonical_name"]],
                "excludeKeywords": [
                    "assistant", "coordinator", "intern", "junior", "finance",
                    "accounting", "legal", "human resources", "recruiter",
                ],
                "maxResults": per_company,
                "maxResultsPerQuery": per_company,
            }
            items = _run(token, payload, max_minutes)
            path = folder / f"company_{target['company_id']}.json"
            path.write_text(
                json.dumps({"input": payload, "items": items}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            paths.append(str(path))
            total += len(items)
            print(
                f"{index}/{len(targets)} company={target['canonical_name']} "
                f"items={len(items)} total={total}", flush=True,
            )
    finally:
        con.close()
    if paths:
        ingest(paths)
    print(f"discovery_items={total} files={len(paths)} folder={folder}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-limit", type=int, default=50)
    parser.add_argument("--per-company", type=int, default=10)
    parser.add_argument("--max-minutes", type=int, default=15)
    args = parser.parse_args()
    raise SystemExit(main(
        max(1, args.company_limit), max(1, min(args.per_company, 10)),
        max(5, args.max_minutes),
    ))
