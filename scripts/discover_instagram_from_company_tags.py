"""Discover verified personal Instagram profiles from official company post tags.

The input people are the separately delivered 100-person LinkedIn cohort. A profile
is accepted only when an official company Instagram post tags a user whose public
profile name matches the verified person identity. Username resemblance is never
used as evidence.
"""
from __future__ import annotations

import json
import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config  # noqa: E402
from mp.db import utcnow  # noqa: E402
from mp.normalize import instagram_handle, normalize_person_name  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.stages.s7_person_verify import instagram_identity_score  # noqa: E402

APP_ID = "936619743392459"
ENDPOINT = "https://www.instagram.com/api/v1/users/web_profile_info/"
SEED = ROOT / "data" / "additional_100_linkedin_final_20260829.json"


def fetch(session: requests.Session, handle: str) -> dict:
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
        return {}
    return ((response.json().get("data") or {}).get("user") or {})


def strong_name_match(profile_name: str, person_name: str) -> bool:
    candidate = normalize_person_name(profile_name).split()
    expected = normalize_person_name(person_name).split()
    if len(candidate) < 2 or len(expected) < 2:
        return False
    return normalize_person_name(profile_name) == normalize_person_name(person_name) or (
        candidate[0] == expected[0] and candidate[-1] == expected[-1]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-qualified", action="store_true")
    args = parser.parse_args()
    seed = json.loads(SEED.read_text(encoding="utf-8"))["rows"]
    ctx = Context.create(db_path="data/master.sqlite")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    minimum = config.sop()["instagram_identity_signals"]["min_score_to_verify"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "data" / "instagram_runs" / f"company_tags_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    audit: list[dict] = []
    changed: set[int] = set()
    people_by_company: dict[str, list[dict]] = defaultdict(list)
    try:
        if args.all_qualified:
            candidates = ctx.db.q(
                """SELECT p.person_id,p.full_name,p.title,p.current_verified,c.company_id,
                          c.canonical_name,c.instagram_url AS company_ig,p.role_relevance
                     FROM people p JOIN companies c USING(company_id)
                     JOIN profiles li ON li.person_id=p.person_id AND li.channel='linkedin'
                    WHERE p.status!='REJECTED' AND p.current_verified=1
                      AND c.status='QUALIFIED' AND c.instagram_url IS NOT NULL
                      AND c.instagram_url!=''
                      AND li.verification_status='VERIFIED'
                      AND NOT EXISTS (
                          SELECT 1 FROM profiles ig WHERE ig.person_id=p.person_id
                            AND ig.channel='instagram' AND ig.verification_status='VERIFIED'
                      )
                    ORDER BY c.canonical_name,p.role_relevance DESC,p.full_name"""
            )
            for row in candidates:
                bucket = people_by_company[row["canonical_name"]]
                if len(bucket) < 10:
                    bucket.append(dict(row))
        else:
            for item in seed:
                linkedin = (item.get("Person LinkedIn URL") or "").rstrip("/").lower()
                row = ctx.db.one(
                    """SELECT p.person_id,p.full_name,p.title,p.current_verified,c.company_id,
                              c.canonical_name,c.instagram_url AS company_ig
                         FROM profiles pr JOIN people p USING(person_id)
                         JOIN companies c USING(company_id)
                        WHERE pr.channel='linkedin' AND lower(rtrim(pr.url,'/'))=?
                          AND p.status!='REJECTED' AND c.status='QUALIFIED'""",
                    (linkedin,),
                )
                if row and row["company_ig"]:
                    people_by_company[row["canonical_name"]].append(dict(row))

        for index, (company, people) in enumerate(sorted(people_by_company.items()), start=1):
            company_handle = instagram_handle(people[0]["company_ig"])
            company_user = fetch(session, company_handle)
            edges = ((company_user.get("edge_owner_to_timeline_media") or {}).get("edges") or [])
            tagged: list[dict] = []
            for edge in edges:
                node = edge.get("node") or {}
                post_url = f"https://www.instagram.com/p/{node.get('shortcode')}/" if node.get("shortcode") else people[0]["company_ig"]
                for tag in ((node.get("edge_media_to_tagged_user") or {}).get("edges") or []):
                    user = (((tag.get("node") or {}).get("user")) or {})
                    if user.get("username"):
                        tagged.append({**user, "post_url": post_url})

            for person in people:
                for tag in tagged:
                    if not strong_name_match(tag.get("full_name") or "", person["full_name"]):
                        continue
                    handle = tag["username"]
                    user = fetch(session, handle)
                    if not user or not strong_name_match(user.get("full_name") or "", person["full_name"]):
                        continue
                    bio = user.get("biography") or ""
                    links = [user["external_url"]] if user.get("external_url") else []
                    for link in user.get("bio_links") or []:
                        if isinstance(link, dict) and link.get("url"):
                            links.append(link["url"])
                    artists = [r["name"] for r in ctx.db.q(
                        "SELECT name FROM artists WHERE company_id=? LIMIT 10",
                        (person["company_id"],),
                    )]
                    score, fired = instagram_identity_score(
                        bio=bio, full_name=user.get("full_name") or "",
                        person=person["full_name"], company=company,
                        external_links=links, company_ig=person["company_ig"] or "",
                        roster_artists=artists,
                    )
                    if not fired.get("company_tagged"):
                        fired["company_tagged"] = True
                        score += config.sop()["instagram_identity_signals"]["company_tagged"]
                    ok = score >= minimum and bool(person["current_verified"])
                    url = f"https://www.instagram.com/{handle}/"
                    if ok:
                        ctx.db.x(
                            """INSERT INTO profiles
                               (person_id,channel,url,handle,verification_status,identity_score,
                                usability,match_signals,verified_at,retrieved_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(person_id,channel) DO UPDATE SET
                                 url=excluded.url,handle=excluded.handle,
                                 verification_status=excluded.verification_status,
                                 identity_score=excluded.identity_score,
                                 usability=excluded.usability,
                                 match_signals=excluded.match_signals,
                                 verified_at=excluded.verified_at,
                                 retrieved_at=excluded.retrieved_at""",
                            (person["person_id"], "instagram", url, handle, "VERIFIED", score,
                             "partial" if user.get("is_private") else "full",
                             ctx.db.js(fired), utcnow(), utcnow()),
                        )
                        ctx.db.x(
                            """INSERT INTO person_evidence
                               (person_id,kind,url,source,snippet,strength,retrieved_at)
                               VALUES (?,?,?,?,?,?,?)
                               ON CONFLICT(person_id,kind,url) DO UPDATE SET
                                 source=excluded.source,snippet=excluded.snippet,
                                 strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                            (person["person_id"], "profile", tag["post_url"],
                             "official_company_instagram_tag",
                             f"Official @{company_handle} post tags @{handle}; profile full name matches {person['full_name']}.",
                             "strongest", utcnow()),
                        )
                        changed.add(int(person["person_id"]))
                    audit.append({
                        "person_id": person["person_id"], "person": person["full_name"],
                        "company": company, "person_instagram_url": url,
                        "company_instagram_url": person["company_ig"],
                        "company_tag_evidence_url": tag["post_url"],
                        "profile_full_name": user.get("full_name") or "", "bio": bio,
                        "identity_score": score, "signals": fired,
                        "result": "VERIFIED" if ok else "UNVERIFIED",
                    })
                    break
            print(f"companies={index}/{len(people_by_company)} verified_people={len(changed)}", flush=True)
            time.sleep(0.8)
    finally:
        ctx.close()

    (out_dir / "company_tag_discovery.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "changed_person_ids.json").write_text(
        json.dumps(sorted(changed)), encoding="utf-8"
    )
    print(json.dumps({"companies": len(people_by_company), "verified": len(changed), "output": str(out_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
