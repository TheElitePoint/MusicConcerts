"""Discover LinkedIn people cheaply, then verify current roles before admission.

The candidate-finder output is discovery evidence only. Every unseen URL is sent
to a separate LinkedIn profile scraper, and a person is stored only when that
fresh profile output explicitly names the already-qualified target company and
the current title passes SS30/SS31. This preserves the SOP hard gate that search
snippets alone cannot verify current employment.
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

from expand_salesnav_batched import ROLE_TITLES, _budget_left, _historical_urls, _human_name  # noqa: E402
from expand_salesnav_filters import _distinctive_name, _employer_key  # noqa: E402
from mp.db import utcnow  # noqa: E402
from mp import config  # noqa: E402
from mp.normalize import linkedin_person_url, normalize_person_name  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402


CANDIDATE_URL = (
    "https://api.apify.com/v2/acts/thirdwatch~linkedin-candidate-finder-scraper/"
    "run-sync-get-dataset-items"
)
PROFILE_URL = (
    "https://api.apify.com/v2/acts/thirdwatch~linkedin-profile-scraper/"
    "run-sync-get-dataset-items"
)
CANDIDATE_ACTOR_ID = "pjkSWP69N9QsEg4DL"
ROLE_QUERY = '(Founder OR Owner OR CEO OR President OR Director OR "Head of" OR Manager)'
DISCOVERY_TITLES = [
    "Founder", "Owner", "CEO", "President", "Managing Director",
    "General Manager", "Marketing Director", "Label Manager",
    "Head of A&R", "Artist Manager",
]


def _candidate_query(company: str) -> str:
    return f'"{company}" {ROLE_QUERY}'


def _company_from_query(query: str) -> str:
    match = re.match(r'\s*"([^"]+)"', query or "")
    return match.group(1) if match else (query or "").strip('" ')


def _post(token: str, url: str, payload: dict[str, Any], timeout: int = 900) -> list[dict[str, Any]]:
    last_error: requests.RequestException | None = None
    for attempt in range(3):
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=timeout,
            )
            if resp.status_code == 402:
                raise RuntimeError("Apify account has insufficient prepaid balance")
            resp.raise_for_status()
            body = resp.json()
            return [x for x in body if isinstance(x, dict)] if isinstance(body, list) else []
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Apify request failed") from last_error


def diagnose_latest() -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(
        "https://api.apify.com/v2/actor-runs",
        headers=headers, params={"limit": 30, "desc": "true"}, timeout=30,
    )
    response.raise_for_status()
    runs = [r for r in ((response.json().get("data") or {}).get("items") or [])
            if r.get("actId") == CANDIDATE_ACTOR_ID]
    for run in runs[:5]:
        print(
            f"{run.get('id')} {run.get('startedAt')} status={run.get('status')} "
            f"dataset={run.get('defaultDatasetId') or '-'} "
            f"message={str(run.get('statusMessage') or '-')[:200]}"
        )
        if run is runs[0]:
            log = requests.get(
                f"https://api.apify.com/v2/logs/{run['id']}",
                headers=headers, timeout=30,
            )
            if log.ok:
                for line in log.text.splitlines()[-25:]:
                    print("  " + line[:300])
    return 0


def _save(kind: str, batch: int, payload: dict[str, Any], items: list[dict[str, Any]]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "data" / "acquisition" / f"thirdwatch_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"batch_{batch:02d}_{kind}.json"
    path.write_text(
        json.dumps({"input": payload, "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _searched_names() -> set[str]:
    out: set[str] = set()
    for path in ROOT.glob("data/acquisition/thirdwatch_*/batch_*_candidates.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8")).get("input") or {}
        except (OSError, ValueError):
            continue
        # Only the combined query+structured-employer method is equivalent to
        # the current acquisition pass. Earlier broad-only and structured-only
        # experiments do not suppress a company.
        if payload.get("searchQueries") and payload.get("currentCompanies"):
            for name in payload.get("currentCompanies") or []:
                out.add(_employer_key(str(name)))
    return out


def _targets(ctx: Context, limit: int, company_ids: set[int] | None = None) -> list[dict[str, Any]]:
    rows = [dict(r) for r in ctx.db.q(
        """SELECT c.company_id,c.canonical_name,c.roster_estimate,
                  (SELECT COUNT(*) FROM people p
                    JOIN profiles pr ON pr.person_id=p.person_id AND pr.channel='linkedin'
                   WHERE p.company_id=c.company_id AND p.status!='REJECTED') active_linkedin,
                  (SELECT COUNT(*) FROM people p JOIN scores s ON s.person_id=p.person_id
                   WHERE p.company_id=c.company_id AND p.status!='REJECTED'
                     AND s.channel='linkedin' AND s.verdict='FINAL PASS') final_people,
                  (SELECT MAX(s.total) FROM people p JOIN scores s ON s.person_id=p.person_id
                   WHERE p.company_id=c.company_id AND p.status!='REJECTED'
                     AND s.channel='linkedin') max_score,
                  (SELECT COUNT(DISTINCT a.signal_type) FROM activity_signals a
                   WHERE a.company_id=c.company_id AND a.observed_at IS NOT NULL) activity_categories
           FROM companies c
          WHERE c.status='QUALIFIED'
            AND (SELECT COUNT(*) FROM people p JOIN profiles pr ON pr.person_id=p.person_id
                  AND pr.channel='linkedin' WHERE p.company_id=c.company_id
                  AND p.status!='REJECTED') < 10
          ORDER BY (final_people>0) DESC, (10-active_linkedin) DESC,
                   COALESCE(max_score,0) DESC, activity_categories DESC, c.company_id"""
    )]
    searched = _searched_names()
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[_employer_key(row["canonical_name"])] += 1
    selected = [row for row in rows
            if _distinctive_name(row["canonical_name"])
            and counts[_employer_key(row["canonical_name"])] == 1
            and _employer_key(row["canonical_name"]) not in searched]
    if company_ids:
        selected = [row for row in selected if int(row["company_id"]) in company_ids]
    return selected[:limit]


def recover_dataset(dataset_id: str, company_limit: int, per_company: int) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    response = requests.get(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items",
        headers={"Authorization": f"Bearer {token}"},
        params={"clean": "true", "format": "json"}, timeout=120,
    )
    response.raise_for_status()
    items = [x for x in response.json() if isinstance(x, dict)]
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        batch = _targets(ctx, company_limit)
    finally:
        ctx.close()
    payload = {
        "searchQueries": [f'"{r["canonical_name"]}"' for r in batch],
        "excludeKeywords": [
            "assistant", "coordinator", "intern", "junior", "finance",
            "accounting", "legal", "human resources", "recruiter",
        ],
        "maxResults": len(batch) * per_company,
        "maxResultsPerQuery": per_company,
    }
    path = _save("candidates", 1, payload, items)
    print(f"Recovered {len(items)} candidates to {path}")
    return 0


def _candidate_url(item: dict[str, Any]) -> str:
    return linkedin_person_url(str(item.get("profileUrl") or item.get("url") or ""))


def _query_company(item: dict[str, Any], batch: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_key = {_employer_key(r["canonical_name"]): r for r in batch}
    query = _company_from_query(str(item.get("searchQuery") or ""))
    key = _employer_key(query)
    if key in by_key:
        return by_key[key]
    matched = item.get("matchedCurrentCompanies") or []
    for value in matched:
        if _employer_key(str(value)) in by_key:
            return by_key[_employer_key(str(value))]
    current = str(item.get("currentCompany") or "")
    return by_key.get(_employer_key(current))


def _name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("fullName") or "").strip()


def _profile_url(item: dict[str, Any]) -> str:
    return linkedin_person_url(str(
        item.get("profile_url") or item.get("profileUrl") or item.get("url") or ""
    ))


def _company_title_pairs(item: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for key in ("current_positions", "currentPositions", "current_roles", "currentRoles"):
        values = item.get(key) or []
        if isinstance(values, dict):
            values = [values]
        for value in values:
            if isinstance(value, dict):
                company = value.get("companyName") or value.get("company") or value.get("name") or ""
                title = value.get("title") or value.get("position") or value.get("role") or ""
                if company:
                    pairs.append((str(company), str(title)))

    companies = item.get("current_companies") or item.get("currentCompanies") or []
    titles = item.get("current_titles") or item.get("currentTitles") or []
    if isinstance(companies, str):
        companies = [companies]
    if isinstance(titles, str):
        titles = [titles]
    for index, company in enumerate(companies):
        if isinstance(company, dict):
            name = company.get("name") or company.get("companyName") or company.get("company") or ""
            title = company.get("title") or company.get("position") or ""
        else:
            name = company
            title = titles[index] if index < len(titles) else (titles[0] if titles else "")
        if name:
            pairs.append((str(name), str(title)))

    company = item.get("currentCompany") or item.get("company") or ""
    title = item.get("currentTitle") or item.get("title") or ""
    if company:
        pairs.append((str(company), str(title)))
    return pairs


def _verified_role(item: dict[str, Any], target: dict[str, Any],
                   candidate: dict[str, Any]) -> tuple[str, str] | None:
    expected = _employer_key(target["canonical_name"])
    for company, title in _company_title_pairs(item):
        if _employer_key(company) == expected and title:
            return company, title
    # Some public profiles expose only a headline. It is usable only when the
    # exact employer is visible in that same fresh profile output.
    headline = str(item.get("headline") or "")
    if expected and expected in _employer_key(headline):
        lower = headline.lower()
        marker = " at "
        title = headline[:lower.rfind(marker)].strip() if marker in lower else headline
        return target["canonical_name"], title
    return None


def main(company_limit: int, batch_size: int, per_company: int,
         account_index: int = 0, company_ids: set[int] | None = None) -> int:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token and account_index > 0:
        tokens = config.keys("apify")
        if account_index > len(tokens):
            raise SystemExit(f"Configured Apify account {account_index} does not exist")
        token = tokens[account_index - 1]
    if not token:
        raise SystemExit("APIFY_TOKEN is required")
    left = _budget_left(token)
    print(f"Apify remaining=${left:.2f}", flush=True)
    if left < 0.25:
        raise SystemExit("Insufficient guarded balance")

    ctx = Context.create(db_path="data/master.sqlite")
    try:
        targets = _targets(ctx, company_limit, company_ids)
        print(f"Qualified distinctive targets: {len(targets)}", flush=True)
        historical = _historical_urls()
        seen = {str(r["url"]).lower() for r in
                ctx.db.q("SELECT url FROM profiles WHERE channel='linkedin'")}
        total_candidates = total_enriched = inserted = 0
        employer_rejects = role_rejects = duplicate_urls = 0

        for start in range(0, len(targets), batch_size):
            batch = targets[start:start + batch_size]
            number = start // batch_size + 1
            candidate_payload = {
                # Structured filters make the company and senior-role signals
                # explicit in the actor's visible match evidence. General quoted
                # queries also surfaced artists and vendors who merely mentioned
                # a label, which is discovery noise rather than current employment.
                "searchQueries": [r["canonical_name"] for r in batch],
                "currentCompanies": [r["canonical_name"] for r in batch],
                "jobTitles": DISCOVERY_TITLES,
                "excludeKeywords": [
                    "assistant", "coordinator", "intern", "junior", "finance",
                    "accounting", "legal", "human resources", "recruiter",
                ],
                "maxResults": len(batch) * per_company,
                "maxResultsPerQuery": len(batch) * per_company,
            }
            candidates = _post(token, CANDIDATE_URL, candidate_payload)
            _save("candidates", number, candidate_payload, candidates)
            total_candidates += len(candidates)

            url_to_candidate: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
            for candidate in candidates:
                target = _query_company(candidate, batch)
                url = _candidate_url(candidate)
                if target is None or not url:
                    continue
                visible = " ".join(str(candidate.get(key) or "") for key in
                                   ("sourceTitle", "sourceSnippet", "headline"))
                if _employer_key(target["canonical_name"]) not in _employer_key(visible):
                    continue
                key = url.lower()
                if key in historical or key in seen:
                    duplicate_urls += 1
                    continue
                url_to_candidate.setdefault(key, (candidate, target))

            urls = [url for url in url_to_candidate]
            profile_payload = {"profileUrls": urls, "maxProfiles": len(urls),
                               "maxConcurrency": 10}
            enriched = _post(token, PROFILE_URL, profile_payload) if urls else []
            _save("profiles", number, profile_payload, enriched)
            total_enriched += len(enriched)
            print(
                f"Batch {number}: candidates={len(candidates)} unseen={len(urls)} "
                f"enriched={len(enriched)}",
                flush=True,
            )

            admitted: dict[int, int] = defaultdict(int)
            for profile in enriched:
                url = _profile_url(profile)
                pair = url_to_candidate.get(url.lower()) if url else None
                if not pair:
                    continue
                candidate, target = pair
                verified = _verified_role(profile, target, candidate)
                if not verified:
                    employer_rejects += 1
                    continue
                employer, title = verified
                name = _name(profile) or _name(candidate)
                if not (_human_name(name) and url):
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
                       (company_id,full_name,name_normalized,title,title_family,
                        seniority,role_relevance,status,first_seen)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(company_id,name_normalized) DO UPDATE SET
                         title=excluded.title,title_family=excluded.title_family,
                         seniority=excluded.seniority,role_relevance=excluded.role_relevance""",
                    (target["company_id"], name, norm, title, role.family,
                     role.seniority, role.relevance, "VERIFIED", utcnow()),
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
                     "VERIFIED", utcnow()),
                )
                if not cur.rowcount:
                    continue
                ctx.db.x(
                    """UPDATE people SET current_verified=1,employment_sources=1,
                              status='VERIFIED',last_researched=? WHERE person_id=?""",
                    (utcnow(), pid),
                )
                ctx.db.x(
                    """UPDATE profiles SET usability='full',verified_at=?
                         WHERE person_id=? AND channel='linkedin'""",
                    (utcnow(), pid),
                )
                ctx.db.x(
                    """INSERT INTO person_evidence
                       (person_id,kind,url,source,snippet,strength,retrieved_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(person_id,kind,url) DO UPDATE SET
                         source=excluded.source,snippet=excluded.snippet,
                         strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                    (pid, "employment", url, "thirdwatch_linkedin_profile",
                     f"LinkedIn current position: {title} at {employer}",
                     "strongest", utcnow()),
                )
                inserted += 1
                admitted[target["company_id"]] += 1
                seen.add(url.lower())

        print(
            f"candidates={total_candidates} enriched={total_enriched} inserted={inserted} "
            f"employer_rejects={employer_rejects} role_rejects={role_rejects} "
            f"duplicate_urls={duplicate_urls}",
            flush=True,
        )
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-limit", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--per-company", type=int, default=10)
    parser.add_argument("--diagnose-latest", action="store_true")
    parser.add_argument("--recover-dataset")
    parser.add_argument("--account-index", type=int, default=0)
    parser.add_argument("--company-ids", default="")
    args = parser.parse_args()
    if args.diagnose_latest:
        raise SystemExit(diagnose_latest())
    if args.recover_dataset:
        raise SystemExit(recover_dataset(
            args.recover_dataset, max(1, args.company_limit),
            max(1, min(args.per_company, 10)),
        ))
    company_ids = {int(value) for value in args.company_ids.split(",") if value.strip()}
    raise SystemExit(main(
        max(1, args.company_limit), max(1, min(args.batch_size, 25)),
        max(1, min(args.per_company, 10)), max(0, args.account_index), company_ids,
    ))
