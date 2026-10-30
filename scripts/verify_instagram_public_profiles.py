"""Verify stored personal Instagram candidates from public profile metadata.

This is a separate Instagram-channel pass. It applies the SOP SS35 identity scorer
to public profile name, bio and external links, preserves evidence for audit, and
never treats username similarity as proof.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config  # noqa: E402
from mp.db import utcnow  # noqa: E402
from mp.normalize import instagram_handle  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.stages.s7_person_verify import instagram_identity_score  # noqa: E402

APP_ID = "936619743392459"
ENDPOINT = "https://www.instagram.com/api/v1/users/web_profile_info/"


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
        return {"http_status": response.status_code}
    payload = response.json()
    user = (payload.get("data") or {}).get("user") or {}
    return {"http_status": response.status_code, "user": user}


def main() -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    min_score = config.sop()["instagram_identity_signals"]["min_score_to_verify"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "data" / "instagram_runs" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    audit: list[dict] = []
    verified = unverified = unavailable = 0
    changed_people: list[int] = []
    try:
        rows = ctx.db.q(
            """SELECT pr.profile_id,pr.person_id,pr.url,pr.verification_status,
                      p.full_name,p.title,p.current_verified,p.status,
                      c.company_id,c.canonical_name,c.instagram_url AS company_ig
                 FROM profiles pr
                 JOIN people p USING(person_id)
                 JOIN companies c USING(company_id)
                WHERE pr.channel='instagram' AND p.status!='REJECTED'
                  AND c.status='QUALIFIED'
                ORDER BY pr.verification_status,c.canonical_name,p.full_name"""
        )
        for index, row in enumerate(rows, start=1):
            handle = instagram_handle(row["url"])
            result = fetch(session, handle)
            user = result.get("user") or {}
            if not user:
                unavailable += 1
                audit.append({
                    "person_id": row["person_id"], "person": row["full_name"],
                    "company": row["canonical_name"], "url": row["url"],
                    "result": "UNAVAILABLE", "http_status": result.get("http_status"),
                })
                continue

            bio = user.get("biography") or ""
            full_name = user.get("full_name") or ""
            links = []
            if user.get("external_url"):
                links.append(user["external_url"])
            for item in user.get("bio_links") or []:
                if isinstance(item, dict) and item.get("url"):
                    links.append(item["url"])
            artists = [r["name"] for r in ctx.db.q(
                "SELECT name FROM artists WHERE company_id=? LIMIT 10",
                (row["company_id"],),
            )]
            score, fired = instagram_identity_score(
                bio=bio,
                full_name=full_name,
                person=row["full_name"],
                company=row["canonical_name"],
                external_links=links,
                company_ig=row["company_ig"] or "",
                roster_artists=artists,
            )
            ok = score >= min_score and bool(row["current_verified"])
            private = bool(user.get("is_private"))
            status = "VERIFIED" if ok else "UNVERIFIED"
            usability = "full" if ok and not private else ("partial" if ok else "weak")
            ctx.db.x(
                """UPDATE profiles SET verification_status=?,identity_score=?,usability=?,
                   match_signals=?,verified_at=? WHERE profile_id=?""",
                (status, score, usability, ctx.db.js(fired), utcnow(), row["profile_id"]),
            )
            evidence_snippet = (
                f"Instagram public profile metadata: full_name={full_name!r}; "
                f"bio={bio[:500]!r}; external_links={links[:5]!r}; identity_score={score}."
            )
            ctx.db.x(
                """INSERT INTO person_evidence
                   (person_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(person_id,kind,url) DO UPDATE SET
                     source=excluded.source,snippet=excluded.snippet,
                     strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                (row["person_id"], "profile", row["url"], "instagram_public_profile",
                 evidence_snippet, "strongest" if ok else "weak", utcnow()),
            )
            changed_people.append(int(row["person_id"]))
            verified += int(ok)
            unverified += int(not ok)
            audit.append({
                "person_id": row["person_id"], "person": row["full_name"],
                "company": row["canonical_name"], "title": row["title"],
                "url": row["url"], "handle": handle, "profile_full_name": full_name,
                "bio": bio, "external_links": links, "followers": user.get("edge_followed_by", {}).get("count"),
                "is_private": private, "identity_score": score, "signals": fired,
                "result": status,
            })
            if index % 10 == 0:
                print(f"checked={index}/{len(rows)} verified={verified}", flush=True)
            time.sleep(0.7)
    finally:
        ctx.close()

    (out_dir / "profile_verification.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "changed_person_ids.json").write_text(
        json.dumps(sorted(set(changed_people))), encoding="utf-8"
    )
    print(json.dumps({
        "checked": len(audit), "verified": verified, "unverified": unverified,
        "unavailable": unavailable, "changed_people": len(set(changed_people)),
        "output": str(out_dir),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
