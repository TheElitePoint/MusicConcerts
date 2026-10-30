"""Find decision-makers at companies through their LinkedIn company page.

Uses harvestapi/linkedin-company-employees (most-used actor of its kind; $0.008 per
full profile) with the company's LinkedIn URL and senior seniority filters, so it
returns only people LinkedIn itself links to that company page. A person is
admitted only when their CURRENT position points at the same company page (slug
match), their title passes SS30/SS31, and they are not on the used-contacts list.
Each company is harvested once (state in data/logs/harvested_companies.json).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import requests  # noqa: E402

from mp import config, used_contacts  # noqa: E402
from mp.db import utcnow  # noqa: E402
from mp.normalize import (is_distinctive_company_name, loose_company_key,  # noqa: E402
                          normalize_person_name)
from mp.pipeline import Context  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402
from mp.sources import apify  # noqa: E402
from mp.stages import s5_people  # noqa: E402

STATE = ROOT / "data/logs/harvested_companies.json"
CONDITIONS = {"company_passed_all_gates": True, "person_has_verified_name": True,
              "person_title_in_role_family": True, "no_cached_profile": True,
              "free_sources_exhausted": True}
# LinkedIn seniority ids: 210 experienced manager, 220 director, 300 VP, 310 CXO,
# 320 owner/partner. Entry-level and individual contributors never qualify (SS31).
SENIORITY = ["210", "220", "300", "310", "320"]


def slug(url: str) -> str:
    m = re.search(r"linkedin\.com/company/([^/?#]+)", url or "", re.I)
    return m.group(1).lower() if m else ""


def match_target(item: dict, by_slug: dict) -> dict | None:
    """The searched company this person CURRENTLY works at, or None.

    Any current position counts (people hold several). A position linked to the
    company page matches by slug. Some employers are typed without a page link
    (companyLinkedinUrl is bare linkedin.com/); those match only when the employer
    name equals the company name, since the search was already scoped to that page.
    """
    searched = (item.get("_meta") or {}).get("query", {}).get("currentCompanies") or []
    scoped = by_slug.get(slug(searched[0])) if searched else None
    if searched and not slug(searched[0]):
        # Searched by NAME (no company page known): accept only an exact employer-name
        # match on a current position -- never LinkedIn's fuzzy company resolution.
        target = by_slug.get("name:" + loose_company_key(searched[0]))
        for pos in item.get("currentPosition") or []:
            if target and loose_company_key(pos.get("companyName", "")) == loose_company_key(target["canonical_name"]):
                return target
        return None
    for pos in item.get("currentPosition") or []:
        page = slug(pos.get("companyLinkedinUrl", ""))
        if page and page in by_slug:
            return by_slug[page]
        if not page and scoped is not None:
            if loose_company_key(pos.get("companyName", "")) == loose_company_key(scoped["canonical_name"]):
                return scoped
    return None


def targets(ctx: Context, done: set[int], limit: int, lane: str = "") -> list:
    rows = ctx.db.q("""SELECT c.company_id, c.canonical_name, c.linkedin_url, c.roster_estimate,
                              c.company_type, c.country,
                              (SELECT COUNT(DISTINCT signal_type) FROM activity_signals a
                                WHERE a.company_id=c.company_id
                                  AND a.observed_at >= date('now','-365 days')) AS cats
                       FROM companies c
                       WHERE c.status='QUALIFIED'
                         AND (c.linkedin_url LIKE '%linkedin.com/company/%'
                              OR (c.company_type='concert_festival' AND c.website IS NOT NULL))
                       ORDER BY cats DESC, c.company_id""")
    out = []
    for r in rows:
        if r["company_id"] in done or used_contacts.company_blocked(r["canonical_name"]):
            continue
        # No name-distinctiveness filter: results are scoped by the company PAGE, so a
        # generic name ("Mad Decent") cannot pull in same-name strangers here.
        if lane == "LIVE" and r["company_type"] != "concert_festival":
            continue
        if r["company_type"] == "concert_festival":
            live = ctx.db.one("""SELECT COUNT(DISTINCT external_id) n FROM activity_signals
                                 WHERE company_id=? AND signal_type='live_event'""",
                              (r["company_id"],))["n"]
            if live < 6 or (r["country"] or "").upper() == "IN":
                continue
        elif not s5_people._worth_paid_search(ctx, r):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def run_async(ctx: Context, actor: str, payload: dict, context: str) -> list | None:
    """Start the run and poll it: a synchronous call times out on large batches
    while the run keeps going server-side and is billed anyway."""
    # Spread runs across accounts (richest first): the actor caps free accounts at
    # 25 profiles per run, so many small runs are needed.
    budgets = {t: apify.account_budget(t, refresh=True) for t in config.keys("apify")}
    token = max(budgets, key=budgets.get, default="")
    if not token or budgets[token] < 0.5:
        return None
    base = "https://api.apify.com/v2"
    run = requests.post(f"{base}/acts/{actor.replace('/', '~')}/runs",
                        params={"token": token}, json=payload, timeout=60).json()["data"]
    while run["status"] in ("READY", "RUNNING"):
        try:
            run = requests.get(f"{base}/actor-runs/{run['id']}",
                               params={"token": token, "waitForFinish": 60}, timeout=90).json()["data"]
        except (requests.RequestException, ValueError, KeyError):
            time.sleep(15)
    cost = float(run.get("usageTotalUsd") or 0)
    ctx.db.log_call("apify", actor, cost=cost, status=201, context=context)
    print(f"   run {run['status']} ${cost:.3f}", flush=True)
    for attempt in range(5):
        try:
            return requests.get(f"{base}/datasets/{run['defaultDatasetId']}/items",
                                params={"token": token, "clean": "true"}, timeout=120).json()
        except (requests.RequestException, ValueError):
            time.sleep(10 * (attempt + 1))
    return []


def main(limit: int, batch: int, per_company: int, lane: str = "") -> int:
    done = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    ctx = Context.create(db_path="data/master.sqlite")
    stats = {"companies": 0, "profiles": 0, "inserted": 0, "wrong_company": 0,
             "role_reject": 0, "used_contact": 0, "duplicate": 0}
    try:
        rows = targets(ctx, done, limit, lane)
        print(f"harvest: {len(rows)} companies", flush=True)
        for start in range(0, len(rows), batch):
            chunk = rows[start:start + batch]
            by_slug = {(slug(r["linkedin_url"] or "") or "name:" + loose_company_key(r["canonical_name"])): r
                       for r in chunk}
            ctx.ledger.require("apify", CONDITIONS)
            items = run_async(ctx, apify.ACTOR_COMPANY_EMPLOYEES, {
                "profileScraperMode": "Full ($8 per 1k)",
                "companies": [r["linkedin_url"] or r["canonical_name"] for r in chunk],
                "companyBatchMode": "one_by_one",
                "maxItemsPerCompany": per_company,
                "maxItems": per_company * len(chunk),
                "seniorityLevelIds": SENIORITY,
            }, context=f"harvest-company-people:{chunk[0]['company_id']}")
            if items is None:
                print("  no account with credit - stopping", flush=True)
                break
            stats["companies"] += len(chunk)
            stats["profiles"] += len(items)
            for item in items:
                target = match_target(item, by_slug)
                if target is None:
                    stats["wrong_company"] += 1
                    continue
                hit = apify._hits_from_harvestapi([item], target["canonical_name"])
                if not hit or not hit[0].linkedin_url or len(hit[0].full_name.split()) < 2:
                    continue
                hit = hit[0]
                if used_contacts.person_blocked(hit.full_name, hit.linkedin_url):
                    stats["used_contact"] += 1
                    continue
                # Client instruction 2026-10-25: no Indian people.
                if "india" in (hit.location or "").lower():
                    stats["india"] = stats.get("india", 0) + 1
                    continue
                role = classify_title(hit.title or "",
                                      small_company=(target["roster_estimate"] or 99) <= 20)
                if not role.qualifies:
                    stats["role_reject"] += 1
                    continue
                if ctx.db.one("SELECT 1 FROM profiles WHERE channel='linkedin' AND lower(url)=lower(?)",
                              (hit.linkedin_url,)):
                    stats["duplicate"] += 1
                    continue
                norm = normalize_person_name(hit.full_name)
                ctx.db.x("""INSERT OR IGNORE INTO people
                            (company_id, full_name, name_normalized, title, title_family,
                             seniority, role_relevance, status, first_seen)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                         (target["company_id"], hit.full_name, norm, hit.title, role.family,
                          role.seniority, role.relevance, "CANDIDATE", utcnow()))
                person = ctx.db.one("SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
                                    (target["company_id"], norm))
                if not person:
                    continue
                pid = person["person_id"]
                ctx.db.x("""INSERT OR IGNORE INTO profiles
                            (person_id, channel, url, handle, verification_status, retrieved_at)
                            VALUES (?,?,?,?,?,?)""",
                         (pid, "linkedin", hit.linkedin_url,
                          hit.linkedin_url.rstrip("/").rsplit("/", 1)[-1], "UNVERIFIED", utcnow()))
                # Source must start with "apify_linkedin": the employer was matched by
                # LinkedIn company-page identity, the strongest employment evidence.
                ctx.db.x("""INSERT INTO person_evidence
                            (person_id, kind, url, source, snippet, strength, retrieved_at)
                            VALUES (?,?,?,?,?,?,?)
                            ON CONFLICT(person_id, kind, url) DO UPDATE SET
                              source=excluded.source, snippet=excluded.snippet,
                              strength=excluded.strength, retrieved_at=excluded.retrieved_at""",
                         (pid, "employment", hit.linkedin_url,
                          "apify_linkedin_company_page" if target["linkedin_url"]
                          else "apify_linkedin_company_name",
                          f"LinkedIn current position: {hit.title} at {hit.company} "
                          f"({target['linkedin_url']})", "strongest", utcnow()))
                if hit.location:
                    ctx.db.x("""INSERT OR IGNORE INTO person_evidence
                                (person_id, kind, url, source, snippet, strength, retrieved_at)
                                VALUES (?,?,?,?,?,?,?)""",
                             (pid, "location", hit.linkedin_url, "apify_linkedin_company_page",
                              hit.location, "strong", utcnow()))
                stats["inserted"] += 1
            done.update(r["company_id"] for r in chunk)
            STATE.write_text(json.dumps(sorted(done)))
            print(f"  batch {start // batch + 1}: {stats}", flush=True)
            if not apify.available(ctx.http):
                print("  apify unavailable - stopping", flush=True)
                break
    finally:
        ctx.close()
    print("done", stats, flush=True)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    # Free accounts get at most 25 profiles per run: 4 companies x 6 people.
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--per-company", type=int, default=6)
    ap.add_argument("--lane", default="", help="LIVE = promoters only")
    a = ap.parse_args()
    raise SystemExit(main(a.limit, a.batch, a.per_company, a.lane))
