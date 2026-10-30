"""Discover staff Instagram accounts through official company following lists."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
ACTOR_URL = (
    "https://api.apify.com/v2/acts/crawlerbros~instagram-follower-scraper/"
    "run-sync-get-dataset-items?format=json&clean=true&timeout=300"
)
EXCLUDED_PERSON_IDS = {172, 383, 384, 501, 502, 748, 1646, 1791, 1792, 1876, 2469, 2470, 2471}


def handle(value: str) -> str:
    parts = [part for part in urlparse(value or "").path.split("/") if part]
    return parts[0].lower() if parts else (value or "").strip().lstrip("@").lower()


def normalized_name(value: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in (value or "").casefold()).split())


def main(company_limit: int, max_items: int, input_json: str = "",
         company_offset: int = 0) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    con = sqlite3.connect(ROOT / "data/master.sqlite")
    con.row_factory = sqlite3.Row
    companies = con.execute(
        """SELECT c.company_id,c.canonical_name,c.instagram_url,COUNT(*) eligible_people
             FROM companies c JOIN people p USING(company_id)
             JOIN scores s ON s.person_id=p.person_id AND s.channel='linkedin'
            WHERE c.status='QUALIFIED' AND c.instagram_url LIKE 'https://www.instagram.com/%'
              AND p.current_verified=1 AND p.status!='REJECTED'
              AND s.verdict='FINAL PASS' AND s.total>=90
              AND p.person_id NOT IN ({})
              AND NOT EXISTS (SELECT 1 FROM profiles ig WHERE ig.person_id=p.person_id
                               AND ig.channel='instagram' AND ig.verification_status='VERIFIED')
            GROUP BY c.company_id
            ORDER BY eligible_people DESC,c.company_id
            LIMIT ? OFFSET ?""".format(",".join("?" for _ in EXCLUDED_PERSON_IDS)),
        (*sorted(EXCLUDED_PERSON_IDS), company_limit, company_offset),
    ).fetchall()
    urls = [row["instagram_url"] for row in companies]
    if input_json:
        items = json.loads(Path(input_json).read_text(encoding="utf-8"))
    else:
        response = requests.post(
            ACTOR_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "usernames": urls,
                "scrapeType": "following",
                "maxFollowersPerProfile": max_items,
            },
            timeout=360,
        )
        response.raise_for_status()
        items = response.json() if isinstance(response.json(), list) else []
    by_handle = {handle(row["instagram_url"]): row for row in companies}
    matches = []
    for item in items:
        source = handle(str(
            item.get("sourceProfile") or item.get("sourceUrl") or item.get("inputUrl")
            or item.get("profile_username") or item.get("profile_url") or ""
        ))
        company = by_handle.get(source)
        if not company:
            continue
        full_name = str(item.get("fullName") or item.get("full_name") or item.get("name") or "").strip()
        username = str(item.get("username") or item.get("handle") or "").strip().lstrip("@")
        if not full_name or not username:
            continue
        people = con.execute(
            """SELECT p.person_id,p.full_name,p.title,s.total
                 FROM people p JOIN scores s ON s.person_id=p.person_id AND s.channel='linkedin'
                WHERE p.company_id=? AND p.current_verified=1 AND p.status!='REJECTED'
                  AND s.verdict='FINAL PASS' AND s.total>=90""",
            (company["company_id"],),
        ).fetchall()
        for person in people:
            if normalized_name(full_name) == normalized_name(person["full_name"]):
                matches.append({
                    "person_id": person["person_id"], "person": person["full_name"],
                    "title": person["title"], "company": company["canonical_name"],
                    "company_instagram_url": company["instagram_url"],
                    "instagram_url": f"https://www.instagram.com/{username}/",
                    "profile_full_name": full_name, "linkedin_score": person["total"],
                    "source": "official_company_instagram_following",
                })
    con.close()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "data/instagram_runs" / f"official_following_apify_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "raw.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "matches.json").write_text(json.dumps(matches, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"companies": len(companies), "items": len(items), "matches": len(matches), "output": str(folder)}))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-limit", type=int, default=10)
    parser.add_argument("--max-items", type=int, default=300)
    parser.add_argument("--input-json", default="")
    parser.add_argument("--company-offset", type=int, default=0)
    args = parser.parse_args()
    raise SystemExit(main(
        max(1, args.company_limit), max(1, args.max_items), args.input_json,
        max(0, args.company_offset),
    ))
