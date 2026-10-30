"""Discover and verify personal Instagram profiles for qualified LinkedIn people.

This is a channel-specific expansion pass governed by SOP SS33/SS35. Search
results and name-shaped handles are candidates only. A candidate is accepted only
when the public Instagram profile has an exact normalized full-name match and the
SS35 identity signals reach the configured verification threshold. Current
employment remains independently verified in the master database.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config  # noqa: E402
from mp.db import utcnow  # noqa: E402
from mp.normalize import instagram_handle, normalize_person_name  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.sources import tavily, websearch  # noqa: E402
from mp.stages.s7_person_verify import instagram_identity_score  # noqa: E402

APP_ID = "936619743392459"
ENDPOINT = "https://www.instagram.com/api/v1/users/web_profile_info/"
IG_URL = re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{3,30})(?:/|\b)", re.I)
AT_HANDLE = re.compile(r"(?<![\w.])@([A-Za-z0-9_.]{3,30})\b")
RESERVED = {
    "p", "reel", "reels", "tv", "stories", "explore", "accounts", "about",
    "developer", "directory", "legal", "privacy", "web", "challenge",
}


def fetch_instagram(session: requests.Session, handle: str) -> tuple[int, dict]:
    try:
        response = session.get(
            ENDPOINT,
            params={"username": handle},
            headers={"x-ig-app-id": APP_ID},
            timeout=25,
        )
        if response.status_code == 429:
            time.sleep(8)
            response = session.get(
                ENDPOINT,
                params={"username": handle},
                headers={"x-ig-app-id": APP_ID},
                timeout=25,
            )
        if not response.ok:
            return response.status_code, {}
        return response.status_code, ((response.json().get("data") or {}).get("user") or {})
    except (requests.RequestException, ValueError):
        return 0, {}


def exact_name_match(profile_name: str, person_name: str) -> bool:
    return bool(profile_name and normalize_person_name(profile_name) == normalize_person_name(person_name))


def ascii_token(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", value.lower())


def derived_handles(person_name: str, linkedin_url: str) -> list[str]:
    tokens = [ascii_token(t) for t in normalize_person_name(person_name).split()]
    tokens = [t for t in tokens if t]
    if len(tokens) < 2:
        return []
    first, last = tokens[0], tokens[-1]
    slug = linkedin_url.rstrip("/").rsplit("/", 1)[-1].lower()
    slug = re.sub(r"-\d+$", "", slug)
    values: list[str] = []
    if not slug.startswith("acw") and re.fullmatch(r"[a-z0-9_.-]+", slug):
        values.extend((slug.replace("-", "."), slug.replace("-", "")))
    values.extend((first + last, first + "." + last, first[0] + last))
    return list(dict.fromkeys(v for v in values if 3 <= len(v) <= 30))


def handles_from_results(results: list) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for result in results:
        blob = " ".join((result.url or "", result.title or "", result.snippet or ""))
        candidates = [(m.group(1), result.url, "instagram_url") for m in IG_URL.finditer(blob)]
        candidates += [(m.group(1), result.url, "search_mention") for m in AT_HANDLE.finditer(blob)]
        for handle, evidence_url, source in candidates:
            key = handle.lower().rstrip(".")
            if key in RESERVED or key in seen:
                continue
            seen.add(key)
            out.append((key, evidence_url, source))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--second-query", action="store_true")
    parser.add_argument("--include-derived", action="store_true")
    parser.add_argument("--discovery-only", action="store_true")
    parser.add_argument("--provider", choices=("free", "tavily"), default="free")
    args = parser.parse_args()

    ctx = Context.create(db_path="data/master.sqlite")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    threshold = int(config.sop()["instagram_identity_signals"]["min_score_to_verify"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "data" / "instagram_runs" / f"tavily_profiles_{stamp}_o{args.offset}"
    out_dir.mkdir(parents=True, exist_ok=True)
    audit: list[dict] = []
    changed: set[int] = set()
    search_count = profile_checks = 0

    try:
        rows = ctx.db.q(
            """SELECT p.person_id,p.full_name,p.title,p.current_verified,p.company_id,
                      c.canonical_name,c.instagram_url AS company_ig,c.domain,
                      li.url AS linkedin_url
                 FROM people p JOIN companies c USING(company_id)
                 JOIN profiles li ON li.person_id=p.person_id AND li.channel='linkedin'
                 JOIN scores sl ON sl.person_id=p.person_id AND sl.channel='linkedin'
                WHERE p.status!='REJECTED' AND p.current_verified=1
                  AND c.status='QUALIFIED' AND li.verification_status='VERIFIED'
                  AND sl.verdict='FINAL PASS'
                  AND NOT EXISTS (
                      SELECT 1 FROM profiles ig WHERE ig.person_id=p.person_id
                        AND ig.channel='instagram' AND ig.verification_status='VERIFIED'
                  )
                ORDER BY sl.total DESC,c.canonical_name,p.role_relevance DESC,p.full_name"""
        )
        rows = rows[args.offset:args.offset + args.limit]

        for index, row in enumerate(rows, start=1):
            query = f'"{row["full_name"]}" "{row["canonical_name"]}" Instagram'
            if args.provider == "tavily":
                results = tavily.search(ctx.http, query, limit=8, context=f"ig-person:{row['person_id']}:company")
            else:
                results = websearch.search(ctx.http, query, limit=8)
            search_count += 1
            if args.second_query and not results:
                query2 = f'"{row["full_name"]}" music Instagram'
                if args.provider == "tavily":
                    results = tavily.search(ctx.http, query2, limit=8, context=f"ig-person:{row['person_id']}:music")
                else:
                    results = websearch.search(ctx.http, query2, limit=8)
                search_count += 1

            handles = handles_from_results(results)
            if args.include_derived:
                handles.extend((h, "", "derived_search_candidate")
                               for h in derived_handles(row["full_name"], row["linkedin_url"]))
            deduped: list[tuple[str, str, str]] = []
            seen_handles: set[str] = set()
            for handle, evidence_url, source in handles:
                if handle.lower() not in seen_handles:
                    seen_handles.add(handle.lower())
                    deduped.append((handle, evidence_url, source))

            if args.discovery_only:
                attempts = [
                    {"handle": handle, "source": source, "evidence_url": evidence_url}
                    for handle, evidence_url, source in deduped[:12]
                ]
                audit.append({
                    "person_id": row["person_id"], "person": row["full_name"],
                    "company": row["canonical_name"], "title": row["title"],
                    "linkedin_url": row["linkedin_url"], "query": query,
                    "search_results": [{"title": r.title, "url": r.url,
                                        "snippet": r.snippet, "backend": r.backend}
                                       for r in results],
                    "attempts": attempts, "person_instagram_url": "",
                    "result": "CANDIDATES ONLY",
                })
                print(json.dumps({"batch": f"{index}/{len(rows)}", "person": row["full_name"],
                                  "candidates": len(deduped), "verified_total": 0}), flush=True)
                continue

            artists = [r["name"] for r in ctx.db.q(
                "SELECT name FROM artists WHERE company_id=? LIMIT 10", (row["company_id"],)
            )]
            attempts: list[dict] = []
            accepted: dict | None = None
            for handle, evidence_url, source in deduped[:12]:
                status, user = fetch_instagram(session, handle)
                profile_checks += 1
                attempt = {
                    "handle": handle, "source": source, "evidence_url": evidence_url,
                    "http_status": status, "found": bool(user),
                    "profile_full_name": user.get("full_name") or "",
                }
                attempts.append(attempt)
                if not user or not exact_name_match(user.get("full_name") or "", row["full_name"]):
                    time.sleep(0.25)
                    continue
                bio = user.get("biography") or ""
                links = [user["external_url"]] if user.get("external_url") else []
                for item in user.get("bio_links") or []:
                    if isinstance(item, dict) and item.get("url"):
                        links.append(item["url"])
                score, fired = instagram_identity_score(
                    bio=bio, full_name=user.get("full_name") or "",
                    person=row["full_name"], company=row["canonical_name"],
                    external_links=links, company_ig=row["company_ig"] or "",
                    roster_artists=artists,
                )
                attempt.update({"bio": bio, "external_links": links,
                                "identity_score": score, "signals": fired})
                if score >= threshold:
                    accepted = {
                        "handle": user.get("username") or handle, "user": user,
                        "score": score, "signals": fired,
                        "evidence_url": evidence_url or f"https://www.instagram.com/{handle}/",
                    }
                    break
                time.sleep(0.25)

            if accepted:
                url = f"https://www.instagram.com/{accepted['handle']}/"
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
                    (row["person_id"], "instagram", url, accepted["handle"], "VERIFIED",
                     accepted["score"], "partial" if accepted["user"].get("is_private") else "full",
                     ctx.db.js(accepted["signals"]), utcnow(), utcnow()),
                )
                ctx.db.x(
                    """INSERT INTO person_evidence
                       (person_id,kind,url,source,snippet,strength,retrieved_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(person_id,kind,url) DO UPDATE SET
                         source=excluded.source,snippet=excluded.snippet,
                         strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                    (row["person_id"], "profile", accepted["evidence_url"],
                     "tavily_instagram_discovery",
                     f"Exact public Instagram full-name match for {row['full_name']}; SS35 identity score {accepted['score']} with independently verified current employment at {row['canonical_name']}.",
                     "strongest", utcnow()),
                )
                changed.add(int(row["person_id"]))

            audit.append({
                "person_id": row["person_id"], "person": row["full_name"],
                "company": row["canonical_name"], "title": row["title"],
                "linkedin_url": row["linkedin_url"], "query": query,
                "search_results": [{"title": r.title, "url": r.url,
                                    "snippet": r.snippet, "backend": r.backend}
                                   for r in results],
                "attempts": attempts,
                "person_instagram_url": (f"https://www.instagram.com/{accepted['handle']}/" if accepted else ""),
                "result": "VERIFIED" if accepted else "NOT VERIFIED",
            })
            print(json.dumps({"batch": f"{index}/{len(rows)}", "person": row["full_name"],
                              "candidates": len(deduped), "verified_total": len(changed)}), flush=True)
    finally:
        ctx.close()

    (out_dir / "discovery.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "changed_person_ids.json").write_text(json.dumps(sorted(changed)), encoding="utf-8")
    print(json.dumps({"people": len(audit), "searches": search_count,
                      "profile_checks": profile_checks, "verified": len(changed),
                      "output": str(out_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
