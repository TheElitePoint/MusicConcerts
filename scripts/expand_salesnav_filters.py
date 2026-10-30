"""Batched Sales Navigator acquisition for already-qualified music companies.

Uses the cookieless, filter-based actor proven in ManageITServices, but keeps all
music-domain policy in this repository. Searches are batched at ten companies;
admission requires an exact normalized current-employer name, a genuine human
name, an SS30 role, no SS31 exclusion, no historical/current URL, and remaining
capacity under the ten-person company ceiling.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from expand_salesnav_batched import (  # noqa: E402
    ROLE_TITLES,
    _budget_left,
    _historical_urls,
    _human_name,
    _person,
    _position,
)
from mp.db import utcnow  # noqa: E402
from mp.normalize import normalize_company_name, normalize_person_name  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402


API_URL = (
    "https://api.apify.com/v2/acts/"
    "bestscrapers~sales-navigator-scraper-by-filters/run-sync-get-dataset-items"
)
INIT_COST = 0.50
RESULT_COST = 0.01
_LEGAL = re.compile(
    r"\b(?:ltd|limited|llc|inc|incorporated|corp|corporation|gmbh|sas|srl|bv|plc)\b",
    re.I,
)


def _employer_key(value: str) -> str:
    return normalize_company_name(_LEGAL.sub(" ", value or ""))


def _distinctive_name(value: str) -> bool:
    """Name-only searches require at least two meaningful words."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", (value or "").lower()) if len(t) >= 2]
    generic = {
        "music", "records", "recordings", "entertainment", "group", "media",
        "sound", "audio", "studio", "studios", "label", "productions", "company",
    }
    return len(tokens) >= 2 and any(len(t) >= 4 and t not in generic for t in tokens)


def _previously_searched_names() -> set[str]:
    out: set[str] = set()
    for path in ROOT.glob("data/acquisition/salesnav_filters_*/batch_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8")).get("input") or {}
        except (OSError, ValueError):
            continue
        for name in payload.get("current_company_names") or []:
            out.add(_employer_key(str(name)))
    return out


def _targets(ctx: Context, limit: int, *, include_searched: bool = False) -> list[dict[str, Any]]:
    rows = [dict(r) for r in ctx.db.q(
        """SELECT c.company_id, c.canonical_name, c.roster_estimate,
                  (SELECT COUNT(*) FROM people p
                    JOIN profiles pr ON pr.person_id=p.person_id AND pr.channel='linkedin'
                   WHERE p.company_id=c.company_id AND p.status!='REJECTED') AS active_linkedin,
                  (SELECT COUNT(*) FROM people p
                    JOIN scores s ON s.person_id=p.person_id AND s.channel='linkedin'
                   WHERE p.company_id=c.company_id AND s.verdict='FINAL PASS'
                     AND p.status!='REJECTED') AS final_people,
                  (SELECT MAX(s.total) FROM people p
                    JOIN scores s ON s.person_id=p.person_id AND s.channel='linkedin'
                   WHERE p.company_id=c.company_id AND p.status!='REJECTED') AS max_score,
                  (SELECT COUNT(DISTINCT a.signal_type) FROM activity_signals a
                   WHERE a.company_id=c.company_id AND a.observed_at IS NOT NULL) AS activity_categories
           FROM companies c
          WHERE c.status='QUALIFIED'
            AND (SELECT COUNT(*) FROM people p
                  JOIN profiles pr ON pr.person_id=p.person_id AND pr.channel='linkedin'
                 WHERE p.company_id=c.company_id AND p.status!='REJECTED') < 10
          ORDER BY (final_people > 0) DESC,
                   (10-active_linkedin) DESC,
                   COALESCE(max_score,0) DESC,
                   activity_categories DESC,
                   c.company_id"""
    )]
    # Exact employer attribution is only safe when a normalized target name is
    # unique across the entire qualified set, not merely within its paid batch.
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[_employer_key(row["canonical_name"])] += 1
    searched = _previously_searched_names()
    unsafe = {
        _employer_key("Animal Music"), _employer_key("Jazz Workshop"),
        _employer_key("Hollywood Records"),
    }
    return [r for r in rows
            if counts[_employer_key(r["canonical_name"])] == 1
            and _distinctive_name(r["canonical_name"])
            and _employer_key(r["canonical_name"]) not in unsafe
            and (include_searched or _employer_key(r["canonical_name"]) not in searched)][:limit]


def _coerce(body: Any) -> dict[str, Any]:
    if isinstance(body, list):
        if len(body) == 1 and isinstance(body[0], dict) and "data" in body[0]:
            return _coerce(body[0])
        if len(body) == 1 and isinstance(body[0], dict) and body[0].get("request_id"):
            item = body[0]
            return {"data": [], "status": "pending",
                    "request_id": item.get("request_id")}
        return {"data": [x for x in body if isinstance(x, dict)],
                "status": "success", "request_id": None}
    if isinstance(body, dict):
        return {
            "data": body.get("data") if isinstance(body.get("data"), list) else [],
            "status": str(body.get("status") or "success").lower(),
            "request_id": body.get("request_id"),
        }
    return {"data": [], "status": "unknown", "request_id": None}


def _post(token: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=360,
    )
    if resp.status_code == 402:
        raise RuntimeError("Apify account has insufficient prepaid balance")
    resp.raise_for_status()
    return _coerce(resp.json())


def _search(token: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = _post(token, payload)
    attempts = 0
    while (
        result["status"] in {"pending", "queued", "running"}
        and not result["data"]
        and result["request_id"]
        and attempts < 20
    ):
        attempts += 1
        time.sleep(30)
        result = _post(token, {"request_id": result["request_id"], "page": 1})
    return result["data"]


def _admit_items(ctx: Context, items: list[dict[str, Any]],
                 batch: list[dict[str, Any]], historical: set[str],
                 seen: set[str]) -> dict[str, int]:
    by_name = {_employer_key(r["canonical_name"]): r for r in batch}
    admitted: dict[int, int] = defaultdict(int)
    stats = {"inserted": 0, "role_rejects": 0,
             "employer_rejects": 0, "duplicate_urls": 0}
    for item in items:
        employer, title, _company_url = _position(item)
        target = by_name.get(_employer_key(employer))
        if target is None:
            stats["employer_rejects"] += 1
            continue
        name, url = _person(item)
        if not (_human_name(name) and url):
            continue
        key = url.lower()
        if key in historical or key in seen:
            stats["duplicate_urls"] += 1
            continue
        role = classify_title(
            title,
            small_company=(target["roster_estimate"] or 99) <= 20,
        )
        if not role.qualifies:
            stats["role_rejects"] += 1
            continue
        capacity = max(0, 10 - int(target["active_linkedin"] or 0))
        if admitted[target["company_id"]] >= capacity:
            continue

        norm = normalize_person_name(name)
        ctx.db.x(
            """INSERT INTO people
               (company_id, full_name, name_normalized, title, title_family,
                seniority, role_relevance, status, first_seen)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(company_id,name_normalized) DO UPDATE SET
                 title=excluded.title, title_family=excluded.title_family,
                 seniority=excluded.seniority, role_relevance=excluded.role_relevance""",
            (target["company_id"], name, norm, title, role.family,
             role.seniority, role.relevance, "CANDIDATE", utcnow()),
        )
        person = ctx.db.one(
            "SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
            (target["company_id"], norm),
        )
        if not person:
            continue
        pid = person["person_id"]
        cur = ctx.db.x(
            """INSERT OR IGNORE INTO profiles
               (person_id,channel,url,handle,verification_status,retrieved_at)
               VALUES (?,?,?,?,?,?)""",
            (pid, "linkedin", url, url.rstrip("/").rsplit("/", 1)[-1],
             "UNVERIFIED", utcnow()),
        )
        if not cur.rowcount:
            continue
        ctx.db.x(
            """INSERT INTO person_evidence
               (person_id,kind,url,source,snippet,strength,retrieved_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(person_id,kind,url) DO UPDATE SET
                 source=excluded.source,snippet=excluded.snippet,
                 strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
            (pid, "employment", url, "apify_salesnav_company_scoped",
             f"LinkedIn current position: {title} at {employer}",
             "strongest", utcnow()),
        )
        stats["inserted"] += 1
        admitted[target["company_id"]] += 1
        seen.add(key)
    return stats


def _save(batch: int, payload: dict[str, Any], items: list[dict[str, Any]]) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "data" / "acquisition" / f"salesnav_filters_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"batch_{batch:02d}.json").write_text(
        json.dumps({"input": payload, "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main(company_limit: int, batch_size: int, result_limit: int,
         max_spend: float, minimum_budget: float) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    try:
        left = _budget_left(token)
    except requests.RequestException as exc:
        raise SystemExit(f"Could not reach Apify: {exc.__class__.__name__}") from None
    safe_cap = min(max_spend, max(0.0, left - 0.20))
    print(f"Apify remaining=${left:.2f}; run spend ceiling=${safe_cap:.2f}", flush=True)
    if left < minimum_budget or safe_cap < INIT_COST + RESULT_COST:
        raise SystemExit("Insufficient guarded budget for a Sales Navigator search")

    ctx = Context.create(db_path="data/master.sqlite")
    try:
        targets = _targets(ctx, company_limit)
        print(f"Qualified unique-name targets: {len(targets)}", flush=True)
        historical = _historical_urls()
        seen = {str(r["url"]).lower() for r in
                ctx.db.q("SELECT url FROM profiles WHERE channel='linkedin'")}
        spent = 0.0
        inserted = role_rejects = employer_rejects = duplicate_urls = 0

        for start in range(0, len(targets), batch_size):
            worst_next = INIT_COST + result_limit * RESULT_COST
            if spent + worst_next > safe_cap:
                print("Spend ceiling reached before next batch", flush=True)
                break
            batch = targets[start:start + batch_size]
            number = start // batch_size + 1
            by_name = {_employer_key(r["canonical_name"]): r for r in batch}
            payload = {
                "current_company_names": [r["canonical_name"] for r in batch],
                "title_keywords": ROLE_TITLES[:20],
                "title_keywords_exclude": [
                    "Assistant", "Coordinator", "Intern", "Junior", "Finance",
                    "Accounting", "Legal", "Human Resources", "Recruiter",
                ],
                "limit": result_limit,
            }
            items = _search(token, payload)
            spent += INIT_COST + len(items) * RESULT_COST
            _save(number, payload, items)
            print(
                f"Batch {number}: {len(batch)} companies, {len(items)} results, "
                f"estimated spend=${spent:.2f}",
                flush=True,
            )
            admitted_stats = _admit_items(ctx, items, batch, historical, seen)
            inserted += admitted_stats["inserted"]
            role_rejects += admitted_stats["role_rejects"]
            employer_rejects += admitted_stats["employer_rejects"]
            duplicate_urls += admitted_stats["duplicate_urls"]

        print(
            f"inserted={inserted} role_rejects={role_rejects} "
            f"employer_rejects={employer_rejects} duplicate_urls={duplicate_urls} "
            f"estimated_spend=${spent:.2f}",
            flush=True,
        )
    finally:
        ctx.close()
    return 0


def resume(raw_path: Path) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    saved = json.loads(raw_path.read_text(encoding="utf-8"))
    payload = saved.get("input") or {}
    receipts = saved.get("items") or []
    request_id = next(
        (str(item.get("request_id")) for item in receipts
         if isinstance(item, dict) and item.get("request_id")),
        "",
    )
    if not request_id:
        raise SystemExit("Resume file contains no request_id")
    names = {_employer_key(str(n)) for n in payload.get("current_company_names") or []}
    print(f"Resuming request for {len(names)} companies", flush=True)
    items = _search(token, {"request_id": request_id, "page": 1})
    print(f"Retrieved {len(items)} results", flush=True)
    _save(1, payload, items)

    ctx = Context.create(db_path="data/master.sqlite")
    try:
        batch = [r for r in _targets(ctx, 10000, include_searched=True)
                 if _employer_key(r["canonical_name"]) in names]
        historical = _historical_urls()
        seen = {str(r["url"]).lower() for r in
                ctx.db.q("SELECT url FROM profiles WHERE channel='linkedin'")}
        stats = _admit_items(ctx, items, batch, historical, seen)
        print(" ".join(f"{key}={value}" for key, value in stats.items()), flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-limit", type=int, default=70)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--result-limit", type=int, default=40)
    parser.add_argument("--max-spend", type=float, default=6.30)
    parser.add_argument("--minimum-budget", type=float, default=1.00)
    parser.add_argument("--resume-file", type=Path)
    args = parser.parse_args()
    if args.resume_file:
        raise SystemExit(resume(args.resume_file))
    raise SystemExit(main(
        max(1, args.company_limit), max(1, min(args.batch_size, 10)),
        max(1, min(args.result_limit, 100)), max(0.0, args.max_spend),
        max(0.0, args.minimum_budget),
    ))
