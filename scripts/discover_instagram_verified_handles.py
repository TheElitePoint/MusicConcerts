"""Resolve Instagram profiles from verified-name handle candidates.

Candidate handles are only a search mechanism. A record is stored only when the
public profile name matches the verified LinkedIn identity AND the profile bio/tag/
external link independently confirms the company relationship. Username resemblance
is never counted as evidence, matching SOP SS35.
"""
from __future__ import annotations

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
from mp.stages.s7_person_verify import instagram_identity_score  # noqa: E402

APP_ID = "936619743392459"
ENDPOINT = "https://www.instagram.com/api/v1/users/web_profile_info/"
SEED = ROOT / "data" / "additional_100_linkedin_final_20260829.json"


def fetch(session: requests.Session, handle: str) -> dict:
    response = session.get(
        ENDPOINT, params={"username": handle},
        headers={"x-ig-app-id": APP_ID}, timeout=20,
    )
    if response.status_code == 429:
        time.sleep(10)
        response = session.get(
            ENDPOINT, params={"username": handle},
            headers={"x-ig-app-id": APP_ID}, timeout=20,
        )
    if not response.ok:
        return {}
    return ((response.json().get("data") or {}).get("user") or {})


def ascii_token(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", value.lower())


def strong_name_match(profile_name: str, person_name: str) -> bool:
    a = normalize_person_name(profile_name).split()
    b = normalize_person_name(person_name).split()
    return bool(len(a) >= 2 and len(b) >= 2 and
                (a == b or (a[0] == b[0] and a[-1] == b[-1])))


def candidates(person_name: str, linkedin_url: str) -> list[str]:
    tokens = [ascii_token(t) for t in normalize_person_name(person_name).split()]
    tokens = [t for t in tokens if t]
    if len(tokens) < 2:
        return []
    first, last = tokens[0], tokens[-1]
    slug = linkedin_url.rstrip("/").rsplit("/", 1)[-1].lower()
    slug = re.sub(r"-\d+$", "", slug)
    out: list[str] = []
    if not slug.lower().startswith("acw") and re.fullmatch(r"[a-z0-9_.-]+", slug):
        out.append(slug.replace("-", "."))
    out.extend((first + last, first + "." + last))
    return list(dict.fromkeys(h for h in out if 3 <= len(h) <= 30))[:3]


def main() -> int:
    seed = json.loads(SEED.read_text(encoding="utf-8"))["rows"]
    ctx = Context.create(db_path="data/master.sqlite")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    minimum = config.sop()["instagram_identity_signals"]["min_score_to_verify"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "data" / "instagram_runs" / f"handle_candidates_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    audit: list[dict] = []
    changed: set[int] = set()
    checked_handles = 0
    try:
        for index, item in enumerate(seed, start=1):
            linkedin = (item.get("Person LinkedIn URL") or "").rstrip("/").lower()
            row = ctx.db.one(
                """SELECT p.person_id,p.full_name,p.title,p.current_verified,c.company_id,
                          c.canonical_name,c.instagram_url AS company_ig,c.domain
                     FROM profiles li JOIN people p USING(person_id)
                     JOIN companies c USING(company_id)
                    WHERE li.channel='linkedin' AND lower(rtrim(li.url,'/'))=?
                      AND p.status!='REJECTED' AND p.current_verified=1
                      AND c.status='QUALIFIED'""",
                (linkedin,),
            )
            if not row:
                continue
            existing = ctx.db.one(
                """SELECT url FROM profiles WHERE person_id=? AND channel='instagram'
                   AND verification_status='VERIFIED'""", (row["person_id"],)
            )
            if existing:
                audit.append({
                    "person_id": row["person_id"], "person": row["full_name"],
                    "company": row["canonical_name"], "result": "ALREADY_VERIFIED",
                    "person_instagram_url": existing["url"], "handles_checked": [],
                })
                continue

            tried: list[dict] = []
            accepted = None
            for handle in candidates(row["full_name"], linkedin):
                user = fetch(session, handle)
                checked_handles += 1
                attempt = {"handle": handle, "found": bool(user),
                           "profile_full_name": user.get("full_name") or ""}
                tried.append(attempt)
                if not user or not strong_name_match(user.get("full_name") or "", row["full_name"]):
                    time.sleep(0.35)
                    continue
                bio = user.get("biography") or ""
                links = [user["external_url"]] if user.get("external_url") else []
                for link in user.get("bio_links") or []:
                    if isinstance(link, dict) and link.get("url"):
                        links.append(link["url"])
                artists = [r["name"] for r in ctx.db.q(
                    "SELECT name FROM artists WHERE company_id=? LIMIT 10", (row["company_id"],)
                )]
                score, fired = instagram_identity_score(
                    bio=bio, full_name=user.get("full_name") or "", person=row["full_name"],
                    company=row["canonical_name"], external_links=links,
                    company_ig=row["company_ig"] or "", roster_artists=artists,
                )
                relationship = any(fired.get(k) for k in (
                    "company_in_bio", "company_tagged", "external_links_confirm",
                    "roster_artists_appear",
                ))
                attempt.update({"bio": bio, "external_links": links,
                                "identity_score": score, "signals": fired,
                                "relationship_confirmed": relationship})
                if score >= minimum and relationship:
                    accepted = {"handle": user.get("username") or handle, "user": user,
                                "score": score, "signals": fired, "bio": bio,
                                "links": links}
                    break
                time.sleep(0.35)

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
                    (row["person_id"], "profile", url, "instagram_public_profile",
                     f"Public profile name matches {row['full_name']}; bio/tag/link confirms {row['canonical_name']} relationship.",
                     "strongest", utcnow()),
                )
                changed.add(int(row["person_id"]))
            audit.append({
                "person_id": row["person_id"], "person": row["full_name"],
                "company": row["canonical_name"], "title": row["title"],
                "linkedin_url": linkedin, "handles_checked": tried,
                "person_instagram_url": (f"https://www.instagram.com/{accepted['handle']}/" if accepted else ""),
                "result": "VERIFIED" if accepted else "NOT VERIFIED",
            })
            if index % 10 == 0:
                print(f"people={index}/{len(seed)} handles={checked_handles} verified={len(changed)}", flush=True)
    finally:
        ctx.close()

    (out_dir / "handle_discovery.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "changed_person_ids.json").write_text(json.dumps(sorted(changed)), encoding="utf-8")
    print(json.dumps({"people": len(seed), "handles_checked": checked_handles,
                      "verified": len(changed), "output": str(out_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
