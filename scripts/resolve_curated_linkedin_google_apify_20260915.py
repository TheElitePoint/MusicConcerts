"""Resolve exact LinkedIn company pages for a curated SOP-qualified cohort."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
ACTOR_URL = "https://api.apify.com/v2/acts/apify~google-search-scraper/run-sync-get-dataset-items?format=json&clean=true&timeout=300"
LI_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([A-Za-z0-9._%\-]+)", re.I)
MUSIC_RE = re.compile(r"\b(record|records|recordings|label|music|artist|roster|album|release|publishing|indie|independent)\b", re.I)


def flat(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def slug_matches(name: str, slug: str) -> bool:
    a, b = flat(name), flat(slug)
    if not a or not b:
        return False
    if a == b:
        return True
    suffixes = ("records", "recordings", "music", "label", "ltd", "limited", "inc", "llc", "group")
    return any(a == b + suffix or b == a + suffix for suffix in suffixes)


def main(ids: set[int], batch_size: int) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in ids)
    rows = con.execute(
        f"SELECT company_id,canonical_name FROM companies WHERE company_id IN ({placeholders}) AND linkedin_url IS NULL ORDER BY company_id",
        tuple(sorted(ids)),
    ).fetchall()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "data/acquisition" / f"company_linkedin_google_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    found = ambiguous = 0
    try:
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            queries = [f'site:linkedin.com/company "{row["canonical_name"]}" record label' for row in batch]
            response = requests.post(
                ACTOR_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={"queries": "\n".join(queries), "maxPagesPerQuery": 1, "mobileResults": False,
                      "includeUnfilteredResults": False, "saveHtml": False,
                      "saveHtmlToKeyValueStore": False, "includeIcons": False},
                timeout=360,
            )
            response.raise_for_status()
            pages = response.json() if isinstance(response.json(), list) else []
            (folder / f"batch_{start//batch_size+1:02d}.json").write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")
            by_term = {}
            for page in pages:
                sq = page.get("searchQuery") or {}
                by_term[str(sq.get("term") or sq.get("query") or page.get("query") or "")] = page
            for row, term in zip(batch, queries):
                matches = []
                for hit in (by_term.get(term) or {}).get("organicResults") or []:
                    blob = " ".join(str(hit.get(k) or "") for k in ("url", "link", "title", "description", "snippet"))
                    for slug in LI_RE.findall(blob):
                        if slug_matches(row["canonical_name"], slug) and MUSIC_RE.search(blob):
                            matches.append(slug.lower())
                unique = list(dict.fromkeys(matches))
                if len(unique) == 1:
                    url = f"https://www.linkedin.com/company/{unique[0]}"
                    con.execute("UPDATE companies SET linkedin_url=?,last_researched=datetime('now') WHERE company_id=?", (url, row["company_id"]))
                    found += 1
                    print(f"found {row['company_id']} {row['canonical_name']} {url}")
                else:
                    ambiguous += 1
                    print(f"unresolved {row['company_id']} {row['canonical_name']} candidates={len(unique)}")
            con.commit()
            print(f"progress={min(start+len(batch),len(rows))}/{len(rows)} found={found}", flush=True)
    finally:
        con.close()
    print(f"companies={len(rows)} found={found} unresolved={ambiguous} raw={folder}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-ids", required=True)
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()
    raise SystemExit(main({int(v) for v in args.company_ids.split(",") if v.strip()}, max(1, min(args.batch_size, 100))))
