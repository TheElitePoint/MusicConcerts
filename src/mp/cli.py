"""Command-line interface: mp <stage> [options]."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config
from .pipeline import Context

STAGES = ("discover", "resolve", "verify", "gate", "people",
          "profiles", "verify-people", "score", "export")


def _run_stages(ctx: Context, stages: list[str], limit: int, lookback: int) -> dict:
    from .stages import (s1_discovery, s2_resolve, s3_verify, s4_gates, s5_people,
                         s6_profiles, s7_person_verify, s8_score, s9_export)
    out: dict[str, dict] = {}
    if "discover" in stages:
        out["discover"] = s1_discovery.run(ctx, lookback_days=lookback)
    if "resolve" in stages:
        out["resolve"] = s2_resolve.run(ctx, limit=limit)
    if "verify" in stages:
        out["verify"] = s3_verify.run(ctx, limit=limit)
    if "gate" in stages:
        out["gate"] = s4_gates.run(ctx, limit=limit)
    if "people" in stages:
        out["people"] = s5_people.run(ctx, limit=limit)
    if "profiles" in stages:
        out["profiles"] = s6_profiles.run(ctx, limit=limit)
    if "verify-people" in stages:
        out["verify-people"] = s7_person_verify.run(ctx, limit=limit)
    if "score" in stages:
        out["score"] = s8_score.run(ctx, limit=limit)
    if "export" in stages:
        out["export"] = s9_export.run(ctx)
        print(f"\n  outputs written to: {ctx.run_dir}")
    return out


def cmd_run(args) -> int:
    stages = list(STAGES) if args.stage == "run" else [args.stage]
    ctx = Context.create(countries=[c.strip().upper() for c in args.countries.split(",")],
                         limit=args.limit, db_path=args.db)
    try:
        results = _run_stages(ctx, stages, args.limit, args.lookback)
        print(f"\nRun {ctx.run_id}")
        for stage, stats in results.items():
            print(f"  {stage:10s} {stats}")
        print(f"\n  budget:     ${ctx.ledger.total_cost():.4f}")
        for provider, s in ctx.ledger.summary().items():
            print(f"    {provider:12s} calls={s['calls']:<5d} cache={s['cache_hits']:<5d} "
                  f"rate={s['cache_rate']:.0%}" + ("  TRIPPED" if s["tripped"] else ""))
    finally:
        ctx.close()
    return 0


def cmd_review_ingest(args) -> int:
    from .stages import review_ingest
    ctx = Context.create(db_path=args.db)
    try:
        stats = review_ingest.run(ctx, args.file)
        print(f"review ingest: {stats}")
        print("Verdicts are permanent -- these entities will be skipped in future runs.")
    finally:
        ctx.close()
    return 0


def cmd_sheets_sync(args) -> int:
    from .sync import sheets
    run_dir = args.run_dir
    if not run_dir:
        runs = sorted(Path("runs").glob("*/"), reverse=True)
        if not runs:
            print("no runs found")
            return 1
        run_dir = runs[0]
    results = sheets.sync(run_dir, args.spreadsheet_id,
                          credentials_path=args.credentials,
                          token_path=args.token, dry_run=args.dry_run)
    for channel, res in results.items():
        print(f"  {channel:10s} {res}")
    return 0


def cmd_status(args) -> int:
    ctx = Context.create(db_path=args.db)
    try:
        print("companies by status:")
        for r in ctx.db.q("""SELECT status, COUNT(*) n FROM companies
                             GROUP BY status ORDER BY n DESC"""):
            print(f"  {r['status']:12s} {r['n']}")
        print("\nrejections by gate:")
        for r in ctx.db.q("""SELECT gate, COUNT(*) n FROM company_rejections
                             GROUP BY gate ORDER BY n DESC"""):
            print(f"  {r['gate']:12s} {r['n']}")
        print("\nqualified companies:")
        for r in ctx.db.q("""SELECT canonical_name, country, company_type, roster_estimate
                             FROM companies WHERE status='QUALIFIED' LIMIT 25"""):
            print(f"  {r['canonical_name'][:34]:36s} {r['country'] or '?':4s} "
                  f"{r['company_type'] or '?':20s} roster={r['roster_estimate']}")
        totals = ctx.db.one("""SELECT COUNT(*) n, COALESCE(SUM(cost),0) c,
                                      SUM(cache_hit) h FROM api_calls""")
        print(f"\nlifetime api calls: {totals['n']} (cache hits {totals['h']}) "
              f"cost ${totals['c']:.4f}")
    finally:
        ctx.close()
    return 0


def cmd_config(args) -> int:
    print(json.dumps({
        "config_hash": config.config_hash(),
        "sop_version": config.sop()["sop_version"],
        "approved_countries": len(config.approved_countries()),
        "keys_present": {p: config.has_key(p)
                         for p in ("spotify", "discogs", "groq", "apify", "tavily")},
    }, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mp",
                                     description="SOP-driven music prospect research")
    parser.add_argument("--db", default="data/master.sqlite")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("run", *STAGES):
        p = sub.add_parser(name, help=f"run the {name} stage")
        p.add_argument("--countries", default="GB",
                       help="comma-separated ISO-2 codes (SS7 approved list)")
        p.add_argument("--limit", type=int, default=0, help="max companies to process")
        p.add_argument("--lookback", type=int, default=120,
                       help="discovery window in days")
        p.set_defaults(func=cmd_run, stage=name)

    ri = sub.add_parser("review-ingest",
                        help="apply human verdicts from a review CSV (permanent)")
    ri.add_argument("file", help="path to companies_review.csv with the verdict column filled")
    ri.set_defaults(func=cmd_review_ingest)

    ss = sub.add_parser("sheets-sync", help="append final sheets to Google Sheets")
    ss.add_argument("spreadsheet_id")
    ss.add_argument("--run-dir", default="", help="defaults to the most recent run")
    ss.add_argument("--credentials", default="credentials.json")
    ss.add_argument("--token", default="token.json")
    ss.add_argument("--dry-run", action="store_true")
    ss.set_defaults(func=cmd_sheets_sync)

    sub.add_parser("status", help="show pipeline state").set_defaults(func=cmd_status)
    sub.add_parser("config", help="show config and key availability").set_defaults(func=cmd_config)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
