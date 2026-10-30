"""Acquire SOP-role LinkedIn candidates with exact LinkedIn company IDs.

The company-ID map below is evidence derived from prior Sales Navigator results
whose displayed current employer exactly matched an already QUALIFIED company.
This avoids the ambiguous name-only matching that produced false positives in
earlier runs.  Admission is still conservative: current company ID, SS30/SS31
role classification, human name, novel personal URL, and the ten-person company
ceiling must all pass before a candidate is written to the master database.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from expand_salesnav_batched import ROLE_TITLES, _budget_left, _human_name, _person, _position  # noqa: E402
from expand_salesnav_filters import API_URL, INIT_COST, RESULT_COST, _coerce  # noqa: E402
from mp import config  # noqa: E402
from mp.db import utcnow  # noqa: E402
from mp.normalize import linkedin_person_url, normalize_person_name  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402


# company_id in our DB -> exact LinkedIn organization ID.  Ambiguous IDs and
# generic-name collisions from the earlier broad searches are intentionally
# omitted.
EXACT_IDS: dict[int, int] = {
    433: 81662,       # Equal Vision Records
    594: 28780130,    # Handwritten Records
    303: 283465,      # Centaur Records
    324: 11409649,    # Frontiers Music Srl
    401: 8827850,     # Flying Nun Records
    1441: 27086013,   # Jazzboy Records
    1865: 28679170,   # Belift Lab
    139: 1668464,     # Cr2 Records
    306: 4146133,     # Compass Records
    316: 52128662,    # The SRG/ILS Group
    679: 9694191,     # Echo Beach
    1012: 9298035,    # Roche Musique
    265: 74927267,    # Father/Daughter Records
    525: 78020010,    # Royal Mountain Records
    855: 235602,      # 8ball Music
    223: 8891434,     # avex entertainment
    330: 18629205,    # Third Man Records
    417: 4112480,     # Easy Star Records
    159: 78331597,    # Chess Club Records
    569: 4026796,     # Hollywood Records
    51: 69779844,     # neon gold records
    169: 1795410,     # City Slang
}

# Public LinkedIn company pages expose their numeric organization ID in the
# visible "all employees" CTA.  Prefer those directly observed IDs over older
# search-result inferences.  Four known parent/wrong-company mappings are
# excluded explicitly after comparing the displayed LinkedIn page name.
_UNSAFE_DIRECTORY_COMPANIES = {217, 252, 462, 922}
_directory_path = ROOT / "data" / "linkedin_company_directory.json"
try:
    _directory_rows = json.loads(_directory_path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    _directory_rows = []
for _entry in _directory_rows:
    _cid = int(_entry.get("company_id") or 0)
    _org = str(_entry.get("linkedin_org_id") or "").strip()
    if _cid and _org.isdigit() and _cid not in _UNSAFE_DIRECTORY_COMPANIES:
        EXACT_IDS[_cid] = int(_org)


def _choose_token() -> tuple[str, float]:
    # Keep explicitly supplied tokens ephemeral rather than adding credentials
    # to project configuration or files.
    supplied = os.environ.get("APIFY_TOKEN", "").strip()
    if supplied:
        try:
            return supplied, _budget_left(supplied)
        except requests.RequestException as exc:
            raise SystemExit(f"Supplied APIFY_TOKEN is not reachable: {exc}") from exc
    best = ("", 0.0)
    for token in config.keys("apify"):
        try:
            remaining = _budget_left(token)
        except requests.RequestException:
            continue
        if remaining > best[1]:
            best = (token, remaining)
    if not best[0]:
        raise SystemExit("No reachable Apify account")
    return best


def _all_delivered_urls() -> set[str]:
    out: set[str] = set()
    for path in ROOT.glob("runs/*/linkedin_final.csv"):
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    url = linkedin_person_url(row.get("Person LinkedIn URL", ""))
                    if url:
                        out.add(url.lower())
        except (OSError, csv.Error):
            continue
    delivered = ROOT / "data" / "net_new_linkedin_final.json"
    try:
        rows = json.loads(delivered.read_text(encoding="utf-8")).get("rows") or []
    except (OSError, ValueError):
        rows = []
    for row in rows:
        url = linkedin_person_url(str(row.get("Person LinkedIn URL") or ""))
        if url:
            out.add(url.lower())
    return out


def _targets(ctx: Context) -> list[dict[str, Any]]:
    marks = ",".join("?" for _ in EXACT_IDS)
    rows = [dict(row) for row in ctx.db.q(
        f"""SELECT c.company_id, c.canonical_name, c.roster_estimate,
                   (SELECT COUNT(*) FROM people p
                     JOIN profiles pr ON pr.person_id=p.person_id AND pr.channel='linkedin'
                    WHERE p.company_id=c.company_id AND p.status!='REJECTED') active_linkedin,
                   (SELECT COUNT(*) FROM people p
                     JOIN scores s ON s.person_id=p.person_id AND s.channel='linkedin'
                    WHERE p.company_id=c.company_id AND p.status!='REJECTED'
                      AND s.verdict='FINAL PASS') final_people
              FROM companies c
             WHERE c.status='QUALIFIED' AND c.company_id IN ({marks})
             ORDER BY active_linkedin ASC, final_people ASC, c.company_id""",
        tuple(EXACT_IDS),
    )]
    return [row for row in rows if int(row["active_linkedin"] or 0) < 10]


def _search(token: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    response = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=360,
    )
    if response.status_code == 402:
        raise RuntimeError("Apify account has insufficient prepaid balance")
    response.raise_for_status()
    result = _coerce(response.json())
    request_id = result.get("request_id")
    attempts = 0
    while result.get("status") in {"pending", "queued", "running"} and request_id and not result.get("data") and attempts < 20:
        attempts += 1
        import time
        time.sleep(30)
        response = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={"request_id": request_id, "page": 1},
            timeout=360,
        )
        response.raise_for_status()
        result = _coerce(response.json())
    return result.get("data") or []


def _save(batch_number: int, payload: dict[str, Any], items: list[dict[str, Any]]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "data" / "acquisition" / f"salesnav_exact_ids_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"batch_{batch_number:02d}.json"
    path.write_text(json.dumps({"input": payload, "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _admit(ctx: Context, items: list[dict[str, Any]], batch: list[dict[str, Any]],
           excluded: set[str], seen: set[str]) -> dict[str, int]:
    by_org = {str(EXACT_IDS[row["company_id"]]): row for row in batch}
    admitted: dict[int, int] = defaultdict(int)
    stats = defaultdict(int)
    for item in items:
        org_id = str(item.get("company_id") or item.get("companyId") or "").strip()
        target = by_org.get(org_id)
        if target is None:
            stats["company_id_rejects"] += 1
            continue
        employer, title, _ = _position(item)
        name, url = _person(item)
        if not (_human_name(name) and url):
            stats["identity_rejects"] += 1
            continue
        url_key = url.lower()
        if url_key in excluded or url_key in seen:
            stats["duplicate_urls"] += 1
            continue
        role = classify_title(title, small_company=(target["roster_estimate"] or 99) <= 20)
        if not role.qualifies:
            stats["role_rejects"] += 1
            continue
        capacity = max(0, 10 - int(target["active_linkedin"] or 0))
        if admitted[target["company_id"]] >= capacity:
            stats["capacity_rejects"] += 1
            continue
        norm = normalize_person_name(name)
        ctx.db.x(
            """INSERT INTO people
               (company_id,full_name,name_normalized,title,title_family,seniority,
                role_relevance,status,first_seen)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(company_id,name_normalized) DO UPDATE SET
                 title=excluded.title,title_family=excluded.title_family,
                 seniority=excluded.seniority,role_relevance=excluded.role_relevance""",
            (target["company_id"], name, norm, title, role.family, role.seniority,
             role.relevance, "CANDIDATE", utcnow()),
        )
        person = ctx.db.one(
            "SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
            (target["company_id"], norm),
        )
        if not person:
            continue
        pid = int(person["person_id"])
        inserted = ctx.db.x(
            """INSERT OR IGNORE INTO profiles
               (person_id,channel,url,handle,verification_status,retrieved_at)
               VALUES (?,?,?,?,?,?)""",
            (pid, "linkedin", url, url.rstrip("/").rsplit("/", 1)[-1],
             "UNVERIFIED", utcnow()),
        )
        if not inserted.rowcount:
            stats["duplicate_urls"] += 1
            continue
        snippet = f"LinkedIn current position: {title} at {employer}; organization ID {org_id}"
        ctx.db.x(
            """INSERT INTO person_evidence
               (person_id,kind,url,source,snippet,strength,retrieved_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(person_id,kind,url) DO UPDATE SET
                 source=excluded.source,snippet=excluded.snippet,
                 strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
            (pid, "employment", url, "apify_salesnav_company_id_scoped",
             snippet, "strongest", utcnow()),
        )
        stats["inserted"] += 1
        admitted[target["company_id"]] += 1
        seen.add(url_key)
    return dict(stats)


def main(batch_size: int, result_limit: int, max_spend: float) -> int:
    token, remaining = _choose_token()
    safe_cap = min(max_spend, max(0.0, remaining - 0.20))
    print(f"Apify remaining=${remaining:.2f}; guarded ceiling=${safe_cap:.2f}", flush=True)
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        targets = _targets(ctx)
        excluded = _all_delivered_urls()
        seen = {str(row["url"]).lower() for row in ctx.db.q("SELECT url FROM profiles WHERE channel='linkedin'")}
        spent = 0.0
        totals = defaultdict(int)
        for start in range(0, len(targets), batch_size):
            remaining_spend = safe_cap - spent
            batch_limit = min(result_limit, int(max(0.0, remaining_spend - INIT_COST) / RESULT_COST))
            if batch_limit < 1:
                print("Spend ceiling reached before next batch", flush=True)
                break
            batch = targets[start:start + batch_size]
            payload = {
                "current_company_ids": [EXACT_IDS[row["company_id"]] for row in batch],
                "title_keywords": ROLE_TITLES[:20],
                "title_keywords_exclude": [
                    "Assistant", "Coordinator", "Intern", "Junior", "Finance",
                    "Accounting", "Legal", "Human Resources", "Recruiter",
                ],
                "limit": batch_limit,
            }
            items = _search(token, payload)
            path = _save(start // batch_size + 1, payload, items)
            spent += INIT_COST + len(items) * RESULT_COST
            stats = _admit(ctx, items, batch, excluded, seen)
            for key, value in stats.items():
                totals[key] += value
            print(
                f"Batch {start // batch_size + 1}: companies={len(batch)} results={len(items)} "
                f"inserted={stats.get('inserted', 0)} estimated_spend=${spent:.2f} raw={path}",
                flush=True,
            )
        print(" ".join(f"{key}={value}" for key, value in sorted(totals.items())), flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--result-limit", type=int, default=100)
    parser.add_argument("--max-spend", type=float, default=3.20)
    args = parser.parse_args()
    raise SystemExit(main(max(1, min(args.batch_size, 10)), max(1, min(args.result_limit, 100)), max(0.0, args.max_spend)))
