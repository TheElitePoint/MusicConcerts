"""Re-run website verification to harvest team pages the first pass never fetched.

91 qualified companies have zero people. They were not skipped for lack of a
website -- every one has a website. They were verified at a time when the crawler
never fetched a contact page and, for many, never reached a team page either, so
`site.team` was empty and no person row was ever created.

Re-running S3 is free (website fetch only) and now yields more than it used to:
the crawler probes /contact, /kontakt and /impressum directly, and
`guess_country` reads company-registration and dialling-code patterns off those
pages -- which is the SS7 evidence 227 backlog companies are missing.

Team-page people are SS38 strongest-tier employment evidence, the tier that lets
SS42 C3 pass without a metered profile call, so this is the cheapest possible
route to a verifiable decision-maker.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mp.pipeline import Context                           # noqa: E402
from mp.stages import s3_verify                           # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--status", default="QUALIFIED")
    ap.add_argument("--max-people", type=int, default=0,
                    help="only companies with at most this many people")
    ap.add_argument("--slice-index", type=int, default=0,
                    help="zero-based worker slice")
    ap.add_argument("--slice-count", type=int, default=1,
                    help="number of parallel worker slices")
    args = ap.parse_args()

    if args.slice_count < 1 or not 0 <= args.slice_index < args.slice_count:
        ap.error("slice-index must be within 0..slice-count-1")

    ctx = Context.create(db_path="data/master.sqlite")
    try:
        rows = ctx.db.q("""SELECT company_id, canonical_name, website, domain,
                                  mbid, discogs_id
                           FROM companies
                           WHERE status = ?
                             AND website IS NOT NULL
                             AND (SELECT COUNT(*) FROM people p
                                   WHERE p.company_id = companies.company_id) <= ?
                           ORDER BY company_id""",
                        (args.status, args.max_people))
        rows = [row for index, row in enumerate(rows)
                if index % args.slice_count == args.slice_index]
        if args.limit:
            rows = rows[:args.limit]

        print(f"re-verifying {len(rows)} companies for team pages", flush=True)
        gained = people_before = 0
        people_before = ctx.db.one("SELECT COUNT(*) n FROM people")["n"]

        for i, row in enumerate(rows, 1):
            before = ctx.db.one("SELECT COUNT(*) n FROM people WHERE company_id=?",
                                (row["company_id"],))["n"]
            try:
                s3_verify.verify_one(ctx, row)
            except Exception as exc:
                print(f"  {i}/{len(rows)} skip {row['canonical_name'][:28]}: "
                      f"{type(exc).__name__}", flush=True)
                continue
            after = ctx.db.one("SELECT COUNT(*) n FROM people WHERE company_id=?",
                               (row["company_id"],))["n"]
            if after > before:
                gained += after - before
                print(f"  {i}/{len(rows)} {row['canonical_name'][:32]:34s} "
                      f"+{after - before} people", flush=True)
            elif i % 20 == 0:
                print(f"  {i}/{len(rows)} ...", flush=True)

        total = ctx.db.one("SELECT COUNT(*) n FROM people")["n"]
        print(f"\npeople {people_before} -> {total} (+{gained})", flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
