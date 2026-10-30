"""Cheaply prioritize Google-discovered profiles before fresh verification.

Thirdwatch search-fallback output is never written as SOP evidence.  It only
selects profiles whose returned public headline still names the target company;
Data Slayer must subsequently return the unmasked current employer and title.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from discover_people_google_apify import _company_visible  # noqa: E402
from mp.normalize import linkedin_person_url  # noqa: E402


BASE = "https://api.apify.com/v2"
ACTOR = "thirdwatch~linkedin-profile-scraper"
OUTPUT = ROOT / "data/thirdwatch_prefilter_pass.json"


def _rows(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT p.person_id,p.company_id,p.full_name,p.title,
                  c.canonical_name AS company,pr.url
             FROM people p
             JOIN companies c ON c.company_id=p.company_id
             JOIN profiles pr ON pr.person_id=p.person_id AND pr.channel='linkedin'
            WHERE c.status='QUALIFIED' AND p.status!='REJECTED'
              AND p.current_verified=0
              AND EXISTS (SELECT 1 FROM person_evidence e
                           WHERE e.person_id=p.person_id
                             AND e.source='apify_google_serp')
            ORDER BY p.role_relevance DESC,p.person_id"""
    ).fetchall()


def _run(token: str, urls: list[str], max_minutes: int) -> tuple[str, list[dict[str, Any]]]:
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.post(
        f"{BASE}/acts/{ACTOR}/runs",
        headers=headers,
        json={
            "profileUrls": urls,
            "maxProfiles": len(urls),
            "maxConcurrency": 10,
            "useFallbacks": True,
            "preferFullProfile": False,
            "usePremiumFallback": False,
            "proxyConfiguration": {"useApifyProxy": False},
        },
        timeout=60,
    )
    response.raise_for_status()
    run_id = str((response.json().get("data") or {}).get("id") or "")
    started = time.monotonic()
    data: dict[str, Any] = {}
    while time.monotonic() - started < max_minutes * 60:
        time.sleep(15)
        response = requests.get(f"{BASE}/actor-runs/{run_id}", headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json().get("data") or {}
        status = str(data.get("status") or "")
        print(f"prefilter_run={run_id} status={status}", flush=True)
        if status in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}:
            break
    if data.get("status") != "SUCCEEDED":
        raise RuntimeError(f"Thirdwatch actor ended with {data.get('status')}")
    response = requests.get(
        f"{BASE}/datasets/{data.get('defaultDatasetId')}/items",
        headers=headers,
        params={"clean": "true", "format": "json"},
        timeout=180,
    )
    response.raise_for_status()
    items = response.json()
    return run_id, items if isinstance(items, list) else []


def _text(item: dict[str, Any]) -> str:
    pieces = [
        item.get("headline"), item.get("title"), item.get("company"),
        item.get("currentCompany"), item.get("current_company"),
    ]
    for key in ("experience", "positions", "currentPositions"):
        value = item.get(key) or []
        if isinstance(value, list):
            for row in value[:3]:
                if isinstance(row, dict):
                    pieces.extend((row.get("title"), row.get("company"), row.get("companyName")))
    return " ".join(str(piece) for piece in pieces if piece)


def main(batch_size: int, max_minutes: int) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    rows = _rows(con)
    con.close()
    by_url = {linkedin_person_url(row["url"]).lower(): row for row in rows if linkedin_person_url(row["url"])}
    print(f"prefilter_candidates={len(rows)}", flush=True)
    all_items: list[dict[str, Any]] = []
    run_ids: list[str] = []
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        run_id, items = _run(token, [row["url"] for row in batch], max_minutes)
        run_ids.append(run_id)
        all_items.extend(items)
        print(f"prefilter_batch={start // batch_size + 1} items={len(items)}", flush=True)
    passed: list[dict[str, Any]] = []
    returned: set[str] = set()
    for item in all_items:
        url = linkedin_person_url(str(
            item.get("linkedinUrl") or item.get("linkedin_url")
            or item.get("profileUrl") or item.get("url") or ""
        ))
        if not url:
            continue
        row = by_url.get(url.lower())
        if row is None:
            continue
        returned.add(url.lower())
        text = _text(item)
        if _company_visible(row["company"], text):
            passed.append({
                "person_id": row["person_id"], "company_id": row["company_id"],
                "company": row["company"], "linkedin_url": url,
                "prefilter_text": text[:500],
            })
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    raw_dir = ROOT / "data/acquisition" / f"thirdwatch_profile_prefilter_{stamp.replace(':', '').replace('+', 'Z')}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "items.json").write_text(json.dumps(all_items, ensure_ascii=False, indent=2), encoding="utf-8")
    OUTPUT.write_text(
        json.dumps({"retrieved_at": stamp, "run_ids": run_ids, "rows": passed}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"returned={len(returned)} passed={len(passed)} missing={len(by_url)-len(returned)} "
        f"output={OUTPUT} raw={raw_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--max-minutes", type=int, default=20)
    args = parser.parse_args()
    raise SystemExit(main(max(1, min(args.batch_size, 1000)), max(5, args.max_minutes)))
