"""Discover decision-makers at QUALIFIED companies nobody has researched yet.

88 qualified companies have never had a single person researched. Every one has a
website and a known roster, and NOT ONE has a LinkedIn company URL -- which is
exactly why they were skipped: `s5_people.run` orders companies without a LinkedIn
URL last, because the metered people-search actor cannot address them at all.

The free paths do not have that limitation, so this script drives the same S5
functions directly at the untouched companies, cheapest source first:

  1. team members already extracted from the company website (SS38 strongest tier)
  2. LinkedIn search-result parsing (SS33) -- name, title, employer, profile URL
  3. company + role queries (SS11/SS33)
  4. Apify, only where free sources came up short and budget remains

The SS51 rule is unchanged: a company yields the people it actually has. Nothing
here pads a company toward a target.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mp import config                                     # noqa: E402
from mp.pipeline import Context                           # noqa: E402
from mp.sources import apify                              # noqa: E402
from mp.stages.s5_people import (                         # noqa: E402
    _apify_people, _classify_existing, _search_linkedin_people, _search_people,
    _trim_to_cap,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-apify", action="store_true",
                    help="free sources only; leaves budget for verification")
    ap.add_argument("--min-people", type=int, default=0,
                    help="only companies with fewer than this many people")
    ap.add_argument("--countries", default="",
                    help="comma-separated ISO-2 filter; paid discovery is worth far "
                         "more in markets with real LinkedIn penetration")
    args = ap.parse_args()

    ctx = Context.create(db_path="data/master.sqlite")
    try:
        rows = ctx.db.q("""SELECT company_id, canonical_name, roster_estimate,
                                  country, linkedin_url, status,
                                  (SELECT COUNT(*) FROM people p
                                    WHERE p.company_id = companies.company_id) AS people
                           FROM companies
                           WHERE status='QUALIFIED'
                             AND (SELECT COUNT(*) FROM people p
                                   WHERE p.company_id = companies.company_id) <= ?
                             AND (? = '' OR country IN (SELECT value FROM
                                   json_each('["' || REPLACE(?, ',', '","') || '"]')))
                           ORDER BY
                             CASE
                               WHEN roster_estimate BETWEEN 3 AND 20 THEN 0
                               WHEN roster_estimate BETWEEN 21 AND 25 THEN 1
                               WHEN roster_estimate BETWEEN 26 AND 30 THEN 2
                               ELSE 3 END,
                             company_id""",
                        (args.min_people, args.countries, args.countries))
        if args.limit:
            rows = rows[:args.limit]

        print(f"mining {len(rows)} qualified companies with <= {args.min_people} people",
              flush=True)
        threshold = config.sources()["people"]["apify_min_free_candidates"]
        totals = {"free": 0, "apify": 0, "companies_with_hits": 0}

        for i, row in enumerate(rows, 1):
            cid, name = row["company_id"], row["canonical_name"]
            roster = row["roster_estimate"]
            try:
                found = _classify_existing(ctx, cid, roster)
                found += _search_linkedin_people(ctx, cid, name, roster)
                found += _search_people(ctx, cid, name, roster)
            except Exception as exc:
                print(f"  {i}/{len(rows)} skip {name[:28]}: {type(exc).__name__}",
                      flush=True)
                continue
            totals["free"] += found

            if not args.no_apify:
                pending = ctx.db.one("""SELECT COUNT(*) n FROM people
                                        WHERE company_id=? AND status='CANDIDATE'""",
                                     (cid,))["n"]
                if pending < threshold and apify.available(ctx.http):
                    try:
                        got = _apify_people(ctx, row, roster)
                        found += got
                        totals["apify"] += got
                    except Exception as exc:
                        print(f"  apify skip {name[:24]}: {type(exc).__name__}",
                              flush=True)

            _trim_to_cap(ctx, cid, roster)
            if found:
                totals["companies_with_hits"] += 1
                print(f"  {i}/{len(rows)} {name[:32]:34s} +{found}", flush=True)
            elif i % 25 == 0:
                print(f"  {i}/{len(rows)} ...", flush=True)

        print(f"\n{totals}", flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
