"""Acquire SOP-qualified LinkedIn people with company-scoped batched searches.

This is deliberately an acquisition layer, not a qualification shortcut:

* only companies already marked QUALIFIED by the music SOP are searched;
* exact LinkedIn company URLs are sent in batches (no generic name search);
* returned current-employer URLs must match the target company URL exactly;
* SS30/SS31 role classification is applied locally;
* historical/current LinkedIn URLs are excluded; and
* at most ten active LinkedIn people are retained per company.

The Apify token is read from ``APIFY_TOKEN`` and is never written to disk.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.normalize import linkedin_person_url, normalize_person_name  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402


ACTOR = "harvestapi~linkedin-profile-search"
API_URL = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"
ACTOR_ID = "M2FMdjRVeF1HPGFcc"

# The actor accepts these as OR alternatives. Local classification remains the
# authority, so a returned title still has to pass the full SS30/SS31 rules.
ROLE_TITLES = [
    "Founder", "Co-Founder", "Owner", "CEO", "President", "Managing Director",
    "Managing Partner", "General Manager", "Head of Digital", "Marketing Director",
    "Head of Marketing", "Head of Artist Marketing", "Head of Audience Development",
    "Head of Label Operations", "Label Manager", "Head of A&R",
    "Head of Artist Development", "Head of Artist Management", "Artist Manager",
    "Head of Artist Services", "Head of Label Services", "Head of Partnerships",
    "Commercial Director", "COO", "Head of Operations", "Operations Director",
]

_NON_PERSON = re.compile(
    r"\b(?:department|team|records|recordings|music|official|management|artists?|"
    r"label|distribution|publishing|services|support)\b",
    re.I,
)


def _company_slug(value: str) -> str:
    match = re.search(r"linkedin\.com/company/([^/?#]+)", value or "", re.I)
    return match.group(1).strip().lower() if match else ""


def _human_name(value: str) -> bool:
    parts = [p for p in re.split(r"\s+", (value or "").strip()) if p]
    return (
        2 <= len(parts) <= 7
        and not _NON_PERSON.search(value or "")
        and all(any(ch.isalpha() for ch in part) for part in parts)
        and len({re.sub(r"\W+", "", part).casefold() for part in parts}) > 1
    )


def _position(item: dict[str, Any]) -> tuple[str, str, str]:
    """Return (company name, title, LinkedIn company URL) for current position."""
    for key in ("currentPosition", "currentPositions"):
        entries = item.get(key) or []
        if isinstance(entries, dict):
            entries = [entries]
        if entries and isinstance(entries[0], dict):
            pos = entries[0]
            return (
                str(pos.get("companyName") or pos.get("company") or "").strip(),
                str(pos.get("position") or pos.get("title") or "").strip(),
                str(pos.get("companyLinkedinUrl") or pos.get("companyUrl") or "").strip(),
            )
    return (
        str(item.get("company") or item.get("currentCompany") or "").strip(),
        str(item.get("job_title") or item.get("title") or item.get("headline") or "").strip(),
        str(item.get("companyLinkedinUrl") or "").strip(),
    )


def _person(item: dict[str, Any]) -> tuple[str, str]:
    name = str(item.get("full_name") or item.get("fullName") or item.get("name") or "").strip()
    if not name:
        name = " ".join(
            p for p in (
                str(item.get("first_name") or item.get("firstName") or "").strip(),
                str(item.get("last_name") or item.get("lastName") or "").strip(),
            ) if p
        )
    url = linkedin_person_url(
        str(item.get("linkedin_url") or item.get("linkedinUrl")
            or item.get("profileUrl") or item.get("url") or "")
    )
    return name, url


BASELINE_RUN_CUTOFF = "20260826T200213Z"


def _historical_urls() -> set[str]:
    out: set[str] = set()
    for path in ROOT.glob("runs/*/linkedin_final.csv"):
        run_stamp = path.parent.name.split("-", 1)[0]
        if run_stamp >= BASELINE_RUN_CUTOFF:
            continue
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    url = linkedin_person_url(row.get("Person LinkedIn URL", ""))
                    if url:
                        out.add(url.lower())
        except (OSError, csv.Error):
            continue
    return out


def _targets(ctx: Context, limit: int) -> list[dict[str, Any]]:
    rows = ctx.db.q(
        """SELECT c.company_id, c.canonical_name, c.linkedin_url, c.roster_estimate,
                  (SELECT COUNT(*) FROM people p
                    JOIN profiles pr ON pr.person_id=p.person_id AND pr.channel='linkedin'
                   WHERE p.company_id=c.company_id AND p.status!='REJECTED') AS active_linkedin,
                  (SELECT COUNT(*) FROM people p
                    JOIN scores s ON s.person_id=p.person_id AND s.channel='linkedin'
                   WHERE p.company_id=c.company_id AND s.verdict='FINAL PASS') AS final_people,
                  (SELECT COUNT(DISTINCT a.signal_type) FROM activity_signals a
                   WHERE a.company_id=c.company_id AND a.observed_at IS NOT NULL) AS activity_categories
           FROM companies c
          WHERE c.status='QUALIFIED'
            AND c.linkedin_url LIKE '%linkedin.com/company/%'
            AND (SELECT COUNT(*) FROM people p
                  JOIN profiles pr ON pr.person_id=p.person_id AND pr.channel='linkedin'
                 WHERE p.company_id=c.company_id AND p.status!='REJECTED') < 10
          ORDER BY final_people DESC, activity_categories DESC, c.company_id
          LIMIT ?""",
        (limit,),
    )
    # A duplicate LinkedIn company slug makes exact attribution ambiguous; omit it.
    by_slug: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_slug[_company_slug(row["linkedin_url"])].append(dict(row))
    return [members[0] for slug, members in by_slug.items() if slug and len(members) == 1]


def _budget_left(token: str) -> float:
    resp = requests.get(
        "https://api.apify.com/v2/users/me/limits",
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    resp.raise_for_status()
    data = (resp.json().get("data") or {})
    current = data.get("current") or {}
    limits = data.get("limits") or {}
    return max(
        0.0,
        float(limits.get("maxMonthlyUsageUsd") or 0)
        - float(current.get("monthlyUsageUsd") or 0),
    )


def _run(token: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    resp = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=360,
    )
    if resp.status_code == 402:
        raise RuntimeError("Apify account has insufficient prepaid balance")
    resp.raise_for_status()
    body = resp.json()
    return [item for item in body if isinstance(item, dict)] if isinstance(body, list) else []


def _diagnose_latest(token: str) -> int:
    """Print non-secret status for the latest runs of this actor."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(
        "https://api.apify.com/v2/actor-runs",
        headers=headers,
        params={"limit": 20, "desc": "true"},
        timeout=30,
    )
    resp.raise_for_status()
    runs = [r for r in ((resp.json().get("data") or {}).get("items") or [])
            if r.get("actId") == ACTOR_ID]
    for run in runs[:4]:
        message = str(run.get("statusMessage") or "").replace("\n", " ")[:240]
        print(f"{run.get('startedAt')} status={run.get('status')} message={message or '-'}")
        log = requests.get(
            f"https://api.apify.com/v2/logs/{run['id']}",
            headers=headers,
            timeout=30,
        )
        if log.ok:
            useful = [line.strip() for line in log.text.splitlines()
                      if any(word in line.lower() for word in
                             ("limit", "upgrade", "error", "failed", "billing", "allowance"))]
            for line in useful[-5:]:
                print("  " + line[:300])
    return 0


def _save_raw(batch_number: int, payload: dict[str, Any], items: list[dict[str, Any]]) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "data" / "acquisition" / f"salesnav_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    # The token is a query parameter in memory only and never appears here.
    (folder / f"batch_{batch_number:02d}.json").write_text(
        json.dumps({"input": payload, "items": items}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def main(company_limit: int, batch_size: int, pages: int, max_items: int,
         minimum_budget: float, omit_title_filter: bool) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("APIFY_TOKEN is required")

    try:
        left = _budget_left(token)
    except requests.RequestException as exc:
        raise SystemExit(f"Could not reach Apify: {exc.__class__.__name__}") from None
    print(f"Apify monthly budget remaining: ${left:.2f}", flush=True)
    if left < minimum_budget:
        raise SystemExit(
            f"Need at least ${minimum_budget:.2f} available for a guarded run; found ${left:.2f}"
        )

    ctx = Context.create(db_path="data/master.sqlite")
    try:
        targets = _targets(ctx, company_limit)
        print(f"Exact LinkedIn-company targets: {len(targets)}", flush=True)
        historical = _historical_urls()
        seen_db = {
            str(r["url"]).lower()
            for r in ctx.db.q("SELECT url FROM profiles WHERE channel='linkedin'")
        }
        inserted = role_rejects = employer_rejects = duplicate_urls = 0

        for start in range(0, len(targets), batch_size):
            batch = targets[start:start + batch_size]
            batch_number = start // batch_size + 1
            by_slug = {_company_slug(r["linkedin_url"]): r for r in batch}
            payload = {
                "currentCompanies": [r["linkedin_url"] for r in batch],
                "profileScraperMode": "Short",
                "takePages": pages,
                "maxItems": max_items,
            }
            if not omit_title_filter:
                payload["currentJobTitles"] = ROLE_TITLES[:20]
            items = _run(token, payload)
            _save_raw(batch_number, payload, items)
            print(f"Batch {batch_number}: {len(batch)} companies, {len(items)} results", flush=True)

            admitted: dict[int, int] = defaultdict(int)
            for item in items:
                company, title, company_url = _position(item)
                target = by_slug.get(_company_slug(company_url))
                if target is None:
                    employer_rejects += 1
                    continue
                name, profile_url = _person(item)
                if not (_human_name(name) and profile_url):
                    continue
                key = profile_url.lower()
                if key in historical or key in seen_db:
                    duplicate_urls += 1
                    continue
                role = classify_title(
                    title,
                    small_company=(target["roster_estimate"] or 99) <= 20,
                )
                if not role.qualifies:
                    role_rejects += 1
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
                       ON CONFLICT(company_id, name_normalized) DO UPDATE SET
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
                       (person_id, channel, url, handle, verification_status, retrieved_at)
                       VALUES (?,?,?,?,?,?)""",
                    (pid, "linkedin", profile_url,
                     profile_url.rstrip("/").rsplit("/", 1)[-1], "UNVERIFIED", utcnow()),
                )
                if not cur.rowcount:
                    continue
                ctx.db.x(
                    """INSERT INTO person_evidence
                       (person_id, kind, url, source, snippet, strength, retrieved_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(person_id, kind, url) DO UPDATE SET
                         source=excluded.source, snippet=excluded.snippet,
                         strength=excluded.strength, retrieved_at=excluded.retrieved_at""",
                    (pid, "employment", profile_url, "apify_salesnav_company_scoped",
                     f"LinkedIn current position: {title} at {company}", "strongest", utcnow()),
                )
                inserted += 1
                admitted[target["company_id"]] += 1
                seen_db.add(key)

        print(
            f"inserted={inserted} role_rejects={role_rejects} "
            f"employer_rejects={employer_rejects} duplicate_urls={duplicate_urls}",
            flush=True,
        )
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-limit", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--max-items", type=int, default=200)
    parser.add_argument("--minimum-budget", type=float, default=1.00)
    parser.add_argument("--omit-title-filter", action="store_true")
    parser.add_argument("--diagnose-latest", action="store_true")
    args = parser.parse_args()
    if args.diagnose_latest:
        token = os.environ.get("APIFY_TOKEN", "").strip()
        if not token:
            raise SystemExit("APIFY_TOKEN is required")
        raise SystemExit(_diagnose_latest(token))
    raise SystemExit(main(
        args.company_limit, max(1, min(args.batch_size, 20)),
        max(1, args.pages), max(1, args.max_items), max(0.0, args.minimum_budget),
        args.omit_title_filter,
    ))
