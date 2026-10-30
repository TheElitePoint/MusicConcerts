"""Backfill LinkedIn company URLs for qualified companies using free search only.

The LinkedIn company URL is the key that unlocks people discovery: the Apify people
search only works when filtered by `currentCompanies` with that URL. Resolving it
here (free) means paid calls are only ever made for companies that can actually
return results.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mp.db import utcnow                    # noqa: E402
from mp.pipeline import Context             # noqa: E402
from mp.stages.s3_verify import _find_company_linkedin  # noqa: E402


def main() -> int:
    ctx = Context.create(countries=["GB"], db_path="data/master.sqlite")
    try:
        rows = ctx.db.q("""SELECT company_id, canonical_name FROM companies
                           WHERE status='QUALIFIED' AND linkedin_url IS NULL
                           ORDER BY company_id""")
        print(f"backfilling {len(rows)} companies", flush=True)
        found = 0
        for row in rows:
            url = _find_company_linkedin(ctx, row["canonical_name"])
            status = url or "-"
            if url:
                ctx.db.x("""UPDATE companies SET linkedin_url=?, last_researched=?
                            WHERE company_id=?""",
                         (url, utcnow(), row["company_id"]))
                found += 1
            print(f"  {row['canonical_name'][:30]:32s} {status}", flush=True)
        print(f"\nfound {found}/{len(rows)}", flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
