"""Recover completed Google-search datasets after a client-side timeout."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATASETS = (
    "ng9G7lyeKGYsJuYmn", "ICUBMW5MtEynHsHy6", "IaE1pjs30LaboK8th",
    "BxnPNKETlz4f7ud19", "SBXDTUbcOpQ2pPCsh",
)
BLOCKED = {"p", "reel", "reels", "stories", "explore", "accounts", "direct", "about", "developer", "tv", "challenge", "web", "legal"}


def profile_url(value: str) -> str:
    from urllib.parse import urlparse
    import re
    parsed = urlparse(value or "")
    if parsed.netloc.lower() not in {"instagram.com", "www.instagram.com"}:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 1 or parts[0].lower() in BLOCKED or not re.fullmatch(r"[A-Za-z0-9._]{1,30}", parts[0]):
        return ""
    return f"https://www.instagram.com/{parts[0]}/"


def main() -> int:
    token = os.environ["APIFY_TOKEN"]
    con = sqlite3.connect(ROOT / "data/master.sqlite")
    con.row_factory = sqlite3.Row
    recovered = []
    for dataset_id in DATASETS:
        response = requests.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            headers={"Authorization": f"Bearer {token}"},
            params={"format": "json", "clean": "true"}, timeout=120,
        )
        response.raise_for_status()
        for page in response.json():
            search = page.get("searchQuery") or {}
            term = str(search.get("term") or search.get("query") or page.get("query") or "")
            found = []
            for hit in page.get("organicResults") or []:
                url = profile_url(str(hit.get("url") or hit.get("link") or ""))
                if url:
                    found.append({"url": url, "title": str(hit.get("title") or ""), "snippet": str(hit.get("description") or hit.get("snippet") or "")})
            quoted = re.search(r'"([^"]+)"', term)
            person_name = quoted.group(1).strip() if quoted else ""
            person = con.execute(
                """SELECT p.person_id,p.full_name,p.title,c.canonical_name company,
                          c.instagram_url company_instagram_url,s.total linkedin_score
                     FROM people p JOIN companies c USING(company_id)
                     JOIN scores s ON s.person_id=p.person_id AND s.channel='linkedin'
                    WHERE lower(p.full_name)=lower(?) AND p.current_verified=1
                      AND p.status!='REJECTED' AND c.status='QUALIFIED'
                      AND s.verdict='FINAL PASS'
                    ORDER BY s.total DESC LIMIT 1""",
                (person_name,),
            ).fetchone()
            recovered.append({**(dict(person) if person else {}), "query": term, "candidates": found})
    con.close()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "data/instagram_runs" / f"google_profile_candidates_recovered_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / "candidates.json"
    output.write_text(json.dumps(recovered, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"queries": len(recovered), "with_candidates": sum(bool(x["candidates"]) for x in recovered), "output": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
