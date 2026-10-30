"""Discover personal Instagram URLs linked from official company team/about pages."""
from __future__ import annotations

import gzip
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config  # noqa: E402
from mp.db import utcnow  # noqa: E402
from mp.normalize import (instagram_handle, instagram_url, normalize_person_name,
                          registrable_domain)  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.stages.s7_person_verify import instagram_identity_score  # noqa: E402

APP_ID = "936619743392459"
ENDPOINT = "https://www.instagram.com/api/v1/users/web_profile_info/"
PAGE_HINT = re.compile(r"(?:^|/)(?:about|team|staff|people|contact|company|who-we-are)", re.I)
PROFILE_LINK = re.compile(r"^https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]+/?(?:\?.*)?$", re.I)


def fetch(session: requests.Session, handle: str) -> dict:
    response = session.get(
        ENDPOINT, params={"username": handle},
        headers={"x-ig-app-id": APP_ID}, timeout=25,
    )
    if response.status_code == 429:
        time.sleep(8)
        response = session.get(
            ENDPOINT, params={"username": handle},
            headers={"x-ig-app-id": APP_ID}, timeout=25,
        )
    if not response.ok:
        return {}
    return ((response.json().get("data") or {}).get("user") or {})


def strong_name_match(profile_name: str, person_name: str) -> bool:
    a = normalize_person_name(profile_name).split()
    b = normalize_person_name(person_name).split()
    return bool(len(a) >= 2 and len(b) >= 2 and
                (a == b or (a[0] == b[0] and a[-1] == b[-1])))


def candidate_links(html: str, person_name: str, company_handle: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    target = normalize_person_name(person_name)
    out: list[str] = []
    for node in soup.find_all(string=True):
        text = " ".join(node.split())
        if not text or target not in normalize_person_name(text):
            continue
        parent = node.parent
        for _ in range(5):
            if parent is None:
                break
            if len(parent.get_text(" ", strip=True)) > 2200:
                break
            for anchor in parent.find_all("a", href=True):
                raw = anchor.get("href", "").split("#", 1)[0]
                if not PROFILE_LINK.match(raw):
                    continue
                url = instagram_url(raw)
                if url and instagram_handle(url) != company_handle and url not in out:
                    out.append(url)
            if out:
                return out
            parent = parent.parent
    return out


def main() -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    minimum = config.sop()["instagram_identity_signals"]["min_score_to_verify"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "data" / "instagram_runs" / f"official_team_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    pages: dict[str, list[tuple[str, str]]] = defaultdict(list)
    audit: list[dict] = []
    changed: set[int] = set()
    try:
        for path in (ROOT / "data" / "cache" / "http").glob("*/*.json.gz"):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    item = json.load(handle)
                url, html = item.get("url", ""), item.get("text", "")
                if (int(item.get("status", 0)) == 200 and "instagram.com/" in html
                        and PAGE_HINT.search(urlparse(url).path)):
                    domain = registrable_domain(url)
                    if domain:
                        pages[domain].append((url, html))
            except (OSError, ValueError, TypeError):
                continue

        rows = ctx.db.q(
            """SELECT p.person_id,p.full_name,p.title,p.current_verified,c.company_id,
                      c.canonical_name,c.website,c.instagram_url AS company_ig
                 FROM people p JOIN companies c USING(company_id)
                 JOIN profiles li ON li.person_id=p.person_id AND li.channel='linkedin'
                WHERE p.status!='REJECTED' AND p.current_verified=1
                  AND c.status='QUALIFIED' AND li.verification_status='VERIFIED'
                  AND c.website IS NOT NULL AND c.website!=''
                  AND NOT EXISTS (
                      SELECT 1 FROM profiles ig WHERE ig.person_id=p.person_id
                        AND ig.channel='instagram' AND ig.verification_status='VERIFIED'
                  )
                ORDER BY c.canonical_name,p.role_relevance DESC"""
        )
        for index, row in enumerate(rows, start=1):
            company_handle = instagram_handle(row["company_ig"] or "")
            found_url = page_url = ""
            for official_url, html in pages.get(registrable_domain(row["website"]), []):
                candidates = candidate_links(html, row["full_name"], company_handle)
                if candidates:
                    found_url, page_url = candidates[0], official_url
                    break
            if not found_url:
                continue
            user = fetch(session, instagram_handle(found_url))
            if not user or not strong_name_match(user.get("full_name") or "", row["full_name"]):
                audit.append({
                    "person_id": row["person_id"], "person": row["full_name"],
                    "company": row["canonical_name"], "candidate_url": found_url,
                    "official_page": page_url, "result": "NAME_MISMATCH_OR_UNAVAILABLE",
                    "profile_full_name": user.get("full_name") or "",
                })
                continue
            bio = user.get("biography") or ""
            links = [user["external_url"]] if user.get("external_url") else []
            artists = [r["name"] for r in ctx.db.q(
                "SELECT name FROM artists WHERE company_id=? LIMIT 10", (row["company_id"],)
            )]
            score, fired = instagram_identity_score(
                bio=bio, full_name=user.get("full_name") or "", person=row["full_name"],
                company=row["canonical_name"], external_links=links,
                company_ig=row["company_ig"] or "", roster_artists=artists,
            )
            if not fired.get("external_links_confirm"):
                fired["external_links_confirm"] = True
                score += config.sop()["instagram_identity_signals"]["external_links_confirm"]
            ok = score >= minimum and bool(row["current_verified"])
            if ok:
                ctx.db.x(
                    """INSERT INTO profiles
                       (person_id,channel,url,handle,verification_status,identity_score,
                        usability,match_signals,verified_at,retrieved_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(person_id,channel) DO UPDATE SET
                         url=excluded.url,handle=excluded.handle,
                         verification_status=excluded.verification_status,
                         identity_score=excluded.identity_score,usability=excluded.usability,
                         match_signals=excluded.match_signals,verified_at=excluded.verified_at,
                         retrieved_at=excluded.retrieved_at""",
                    (row["person_id"], "instagram", found_url, instagram_handle(found_url),
                     "VERIFIED", score, "partial" if user.get("is_private") else "full",
                     ctx.db.js(fired), utcnow(), utcnow()),
                )
                ctx.db.x(
                    """INSERT INTO person_evidence
                       (person_id,kind,url,source,snippet,strength,retrieved_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(person_id,kind,url) DO UPDATE SET
                         source=excluded.source,snippet=excluded.snippet,
                         strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                    (row["person_id"], "profile", page_url, "official_team_instagram_link",
                     f"Official company page links {row['full_name']} to {found_url}.",
                     "strongest", utcnow()),
                )
                changed.add(int(row["person_id"]))
            audit.append({
                "person_id": row["person_id"], "person": row["full_name"],
                "company": row["canonical_name"], "person_instagram_url": found_url,
                "official_page": page_url, "profile_full_name": user.get("full_name") or "",
                "bio": bio, "identity_score": score, "signals": fired,
                "result": "VERIFIED" if ok else "UNVERIFIED",
            })
            if index % 25 == 0:
                print(f"people={index}/{len(rows)} verified={len(changed)}", flush=True)
            time.sleep(0.7)
    finally:
        ctx.close()

    (out_dir / "official_team_discovery.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "changed_person_ids.json").write_text(json.dumps(sorted(changed)), encoding="utf-8")
    print(json.dumps({"people": len(rows), "candidates": len(audit), "verified": len(changed), "output": str(out_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
