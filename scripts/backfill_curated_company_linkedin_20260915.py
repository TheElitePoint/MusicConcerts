"""Resolve LinkedIn company pages for a strict, pre-audited music-label cohort."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.stages.s3_verify import _find_company_linkedin  # noqa: E402


def main(ids: set[int]) -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = ctx.db.q(
            f"""SELECT company_id,canonical_name,linkedin_url
                  FROM companies WHERE company_id IN ({placeholders})
                  ORDER BY company_id""",
            tuple(sorted(ids)),
        )
        found = existing = 0
        for row in rows:
            if row["linkedin_url"]:
                existing += 1
                print(f"existing {row['company_id']} {row['canonical_name']} {row['linkedin_url']}")
                continue
            url = _find_company_linkedin(ctx, row["canonical_name"])
            if url:
                ctx.db.x(
                    "UPDATE companies SET linkedin_url=?,last_researched=? WHERE company_id=?",
                    (url, utcnow(), row["company_id"]),
                )
                found += 1
                print(f"found {row['company_id']} {row['canonical_name']} {url}")
            else:
                print(f"missing {row['company_id']} {row['canonical_name']}")
        print(f"companies={len(rows)} existing={existing} found={found} missing={len(rows)-existing-found}")
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-ids", required=True)
    args = parser.parse_args()
    raise SystemExit(main({int(value) for value in args.company_ids.split(",") if value.strip()}))
