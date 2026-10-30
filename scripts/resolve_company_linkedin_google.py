"""Resolve company LinkedIn pages via Google results (Apify google-search-scraper).

Company-scoped people search is only as good as the company LinkedIn URL it is
given -- a company NAME resolves to whichever page LinkedIn picks ("Kscope" ->
an AI startup). A URL is accepted only when BOTH hold: the slug closely matches the
company name (s3's strict similarity check) AND the result reads like a music
company or mentions the company's own domain. $0.0045 per query.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import requests  # noqa: E402

from mp import config  # noqa: E402
from mp.db import utcnow  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.sources import apify  # noqa: E402
from mp.stages.s3_verify import _MUSIC_CONTEXT, _company_slug_matches  # noqa: E402

ACTOR = "apify/google-search-scraper"
BY_DOMAIN = False   # --by-domain: search the company's own domain instead of its name
LI = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([A-Za-z0-9\-_%.]+)", re.I)
# Promoters describe themselves as concerts / live / events, not records or labels.
_LIVE_CONTEXT = re.compile(r"\b(concerts?|promot\w*|live (?:music|events|entertainment)|"
                           r"festivals?|touring|tours|events?|entertainment|gigs?|shows)\b", re.I)
CONDITIONS = {"company_passed_all_gates": True, "person_has_verified_name": True,
              "person_title_in_role_family": True, "no_cached_profile": True,
              "free_sources_exhausted": True}


def pick(name: str, domain: str, results: list[dict]) -> str:
    for res in results:
        m = LI.match(res.get("url") or "")
        if not m:
            continue
        slug = m.group(1).split("?")[0].rstrip("/")
        blob = f"{res.get('title', '')} {res.get('description', '')}"
        if not _company_slug_matches(name, slug):
            continue
        if (_MUSIC_CONTEXT.search(blob) or _LIVE_CONTEXT.search(blob)
                or (domain and domain.lower() in blob.lower())):
            return f"https://www.linkedin.com/company/{slug.lower()}"
    return ""


def run_async(token: str, payload: dict) -> list[dict]:
    """Start a run and poll it. A synchronous call times out on large batches while
    the run keeps going server-side and is billed, so the results must be collected."""
    base = "https://api.apify.com/v2"
    run = requests.post(f"{base}/acts/{ACTOR.replace('/', '~')}/runs",
                        params={"token": token}, json=payload, timeout=60).json()["data"]
    while run["status"] in ("READY", "RUNNING"):
        try:
            run = requests.get(f"{base}/actor-runs/{run['id']}",
                               params={"token": token, "waitForFinish": 60}, timeout=90).json()["data"]
        except (requests.RequestException, ValueError, KeyError):
            time.sleep(15)
    for attempt in range(5):
        try:
            return requests.get(f"{base}/datasets/{run['defaultDatasetId']}/items",
                                params={"token": token, "clean": "true"}, timeout=120).json()
        except (requests.RequestException, ValueError):
            time.sleep(10 * (attempt + 1))
    return []


def resolve(ctx: Context, rows: list, *, write: bool, items: list | None = None) -> dict[int, str]:
    queries = [(f'site:linkedin.com/company "{r["domain"]}"' if BY_DOMAIN and r["domain"]
                else f'site:linkedin.com/company "{r["canonical_name"]}"') for r in rows]
    if items is None:
        ctx.ledger.require("apify", CONDITIONS)
        token = next(t for t in config.keys("apify") if apify.account_budget(t, refresh=True) >= 0.5)
        items = run_async(token, {
            "queries": "\n".join(queries), "maxPagesPerQuery": 1, "resultsPerPage": 10,
            "mobileResults": False, "saveHtml": False, "saveHtmlToKeyValueStore": False,
        })
        ctx.db.log_call("apify", ACTOR, cost=0.0045 * len(queries), status=201,
                        context=f"company-linkedin-google:{len(rows)}")
    by_query = {(it.get("searchQuery") or {}).get("term", ""): it.get("organicResults") or []
                for it in items}
    found: dict[int, str] = {}
    for r, q in zip(rows, queries):
        url = pick(r["canonical_name"], r["domain"] or "", by_query.get(q, []))
        if url:
            found[r["company_id"]] = url
            if write:
                ctx.db.x("UPDATE companies SET linkedin_url=?, last_researched=? WHERE company_id=?",
                         (url, utcnow(), r["company_id"]))
    return found


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("company_ids", nargs="+", type=int)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--by-domain", action="store_true")
    args = ap.parse_args()
    BY_DOMAIN = args.by_domain
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        marks = ",".join("?" * len(args.company_ids))
        rows = ctx.db.q(f"SELECT company_id, canonical_name, domain, linkedin_url FROM companies "
                        f"WHERE company_id IN ({marks})", tuple(args.company_ids))
        rows = sorted(rows, key=lambda r: args.company_ids.index(r["company_id"]))
        for chunk in range(0, len(rows), 100):
            batch = rows[chunk:chunk + 100]
            found = resolve(ctx, batch, write=args.write)
            for r in batch:
                print(f"{r['company_id']}\t{r['canonical_name'][:30]}\t{r['linkedin_url'] or '-'}\t"
                      f"{found.get(r['company_id'], '-')}", flush=True)
    finally:
        ctx.close()
