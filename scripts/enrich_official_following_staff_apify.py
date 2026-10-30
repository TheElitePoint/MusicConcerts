"""Find potential label staff within official-company following datasets."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.verify_instagram_candidates_apify import _items, _start, _wait  # noqa: E402

ROLE_RE = re.compile(
    r"\b(?:founder|co-founder|owner|president|ceo|chief|head|director|manager|"
    r"a\s*&\s*r|a&r|marketing|operations|partnerships|label)\b", re.I
)


def main(input_json: str, max_handles: int, max_minutes: int, handle_offset: int) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    rows = json.loads(Path(input_json).read_text(encoding="utf-8"))
    attribution: dict[str, set[str]] = {}
    for row in rows:
        username = str(row.get("username") or "").strip().lower()
        source = str(row.get("profile_username") or "").strip().lower()
        if username and source:
            attribution.setdefault(username, set()).add(source)
    handles = list(attribution)[handle_offset:handle_offset + max_handles]
    run_id = _start(token, handles)
    run = _wait(token, run_id, max_minutes)
    if run.get("status") != "SUCCEEDED":
        raise RuntimeError(f"Instagram profile actor ended with {run.get('status')}")
    items = _items(token, str(run.get("defaultDatasetId") or ""))
    leads = []
    for item in items:
        username = str(item.get("username") or "").strip().lower()
        full_name = str(item.get("fullName") or "").strip()
        bio = str(item.get("biography") or "").strip()
        if len(full_name.split()) < 2 or not ROLE_RE.search(bio):
            continue
        for source in sorted(attribution.get(username, ())):
            company_token = source.replace("records", "").replace("music", "").replace("_", "").replace(".", "")
            compact_bio = re.sub(r"[^a-z0-9]", "", bio.lower())
            if f"@{source}" not in bio.lower() and len(company_token) >= 4 and company_token not in compact_bio:
                continue
            leads.append({
                "full_name": full_name, "username": username,
                "instagram_url": f"https://www.instagram.com/{username}/",
                "bio": bio, "external_url": item.get("externalUrl") or "",
                "company_instagram_handle": source,
                "followers": item.get("followersCount"), "following": item.get("followsCount"),
                "posts": item.get("postsCount"), "private": bool(item.get("private") or item.get("isPrivate")),
            })
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "data/instagram_runs" / f"official_following_staff_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "raw_profiles.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "staff_leads.json").write_text(json.dumps(leads, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"requested": len(handles), "returned": len(items), "staff_leads": len(leads), "output": str(folder)}))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input_json")
    parser.add_argument("--max-handles", type=int, default=250)
    parser.add_argument("--max-minutes", type=int, default=30)
    parser.add_argument("--handle-offset", type=int, default=0)
    args = parser.parse_args()
    raise SystemExit(main(
        args.input_json, max(1, args.max_handles), max(5, args.max_minutes),
        max(0, args.handle_offset),
    ))
