"""Bulk current-employer/title verification with a pay-per-result actor."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from mp.normalize import linkedin_slug  # noqa: E402
from verify_profiles_apimaestro import _apply, _candidates, _items, _wait  # noqa: E402


BASE = "https://api.apify.com/v2"
ACTOR = "automation-lab~linkedin-profile-scraper"


def _start(token: str, urls: list[str]) -> str:
    response = requests.post(
        f"{BASE}/acts/{ACTOR}/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={"profileUrls": urls, "maxProfiles": len(urls)},
        timeout=60,
    )
    response.raise_for_status()
    return str((response.json().get("data") or {}).get("id") or "")


def _save(run_id: str, items: list[dict[str, Any]]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "data/acquisition" / f"automation_lab_profile_verification_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"run_{run_id}.json"
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _flatten(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        url = str(item.get("linkedinUrl") or item.get("linkedin_url") or item.get("profileUrl") or "")
        _kind, slug = linkedin_slug(url)
        if slug:
            out[slug.lower()] = item
    return out


def main(limit: int, max_minutes: int) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    try:
        rows = _candidates(con, limit)
        urls = [row["url"] for row in rows]
        print(f"verification_candidates={len(rows)}", flush=True)
        if not urls:
            return 0
        run_id = _start(token, urls)
        if not run_id:
            raise RuntimeError("Apify did not return a run ID")
        print(f"verification_run id={run_id} status=STARTED", flush=True)
        run = _wait(token, run_id, max_minutes)
        if run.get("status") != "SUCCEEDED":
            raise RuntimeError(f"Verification actor ended with {run.get('status')}")
        items = _items(token, str(run.get("defaultDatasetId") or ""))
        path = _save(run_id, items)
        profiles = _flatten(items)
        stats = _apply(con, rows, profiles)
        print(f"dataset_items={len(items)} profiles={len(profiles)} raw={path}", flush=True)
        print(" ".join(f"{key}={value}" for key, value in stats.items()), flush=True)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-minutes", type=int, default=45)
    args = parser.parse_args()
    raise SystemExit(main(max(0, min(args.limit, 1000)), max(5, args.max_minutes)))
