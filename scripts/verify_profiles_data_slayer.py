"""Verify current LinkedIn employer/title with Data Slayer's fresh profile actor."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from mp.normalize import linkedin_slug  # noqa: E402
from verify_profiles_apimaestro import _apply, _candidates, _items, _wait  # noqa: E402


BASE = "https://api.apify.com/v2"
ACTOR = "data-slayer~linkedin-profile-scraper"


def _start(token: str, urls: list[str]) -> str:
    response = requests.post(
        f"{BASE}/acts/{ACTOR}/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={"linkedin_urls": urls},
        timeout=60,
    )
    response.raise_for_status()
    return str((response.json().get("data") or {}).get("id") or "")


def _save(run_id: str, items: list[dict[str, Any]]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "data/acquisition" / f"data_slayer_profile_verification_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"run_{run_id}.json"
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _flatten(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        url = str(
            item.get("profile_link") or item.get("linkedin_url")
            or item.get("linkedinUrl") or item.get("profileUrl") or ""
        )
        _kind, slug = linkedin_slug(url)
        if not slug:
            slug = str(item.get("username") or item.get("public_identifier") or "").strip()
        if not slug:
            continue
        # Normalize the top-level current role into the shared verifier shape.
        title = str(item.get("job_title") or item.get("current_job_title") or "").strip()
        company = str(item.get("current_company_name") or item.get("company_name") or "").strip()
        if title and company:
            item = dict(item)
            item["currentPositions"] = [{"title": title, "company": company, "isCurrent": True}]
        out[unquote(slug).lower()] = item
    return out


def main(limit: int, max_minutes: int, ids_json: str = "", min_prescore: int = 0) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    try:
        rows = _candidates(con, limit)
        if ids_json:
            payload = json.loads(Path(ids_json).read_text(encoding="utf-8"))
            allowed = {int(row["person_id"]) for row in payload.get("rows", [])}
            rows = [row for row in rows if int(row["person_id"]) in allowed]
        if min_prescore > 0:
            allowed = {
                int(row["person_id"])
                for row in con.execute(
                    "SELECT person_id FROM scores WHERE channel='linkedin' AND total>=?",
                    (min_prescore,),
                )
            }
            rows = [row for row in rows if int(row["person_id"]) in allowed]
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
    parser.add_argument("--ids-json", default="")
    parser.add_argument("--min-prescore", type=int, default=0)
    args = parser.parse_args()
    raise SystemExit(main(
        max(0, min(args.limit, 1000)), max(5, args.max_minutes),
        args.ids_json.strip(), max(0, min(args.min_prescore, 100)),
    ))
