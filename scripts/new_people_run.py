"""A full pass aimed at NEW people, using the capacity that actually remains.

Company people-search is exhausted on the accounts that still have budget, so this
run leans on:
  * free LinkedIn-SERP discovery (Tavily) to FIND people, and
  * the cheap batch profile fetch ($0.004) to turn their snippet evidence into the
    first-party current-position claim SS42 C3 demands.
Anything already cached is re-scored and exported rather than re-fetched.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow                                          # noqa: E402
from mp.pipeline import Context                                   # noqa: E402
from mp.stages import (s5_people, s6_profiles, s7_person_verify,  # noqa: E402
                       s8_score, s9_export)
from mp.stages.s3_verify import _find_company_linkedin            # noqa: E402

ENV = {**os.environ, "PYTHONPATH": "src"}


def log(m): print(m, flush=True)


def main() -> int:
    ctx = Context.create(countries=["GB", "US", "IE", "AU"], db_path="data/master.sqlite")
    try:
        # 1. LinkedIn company URLs unlock people discovery; resolving them is free.
        rows = ctx.db.q("""SELECT company_id, canonical_name FROM companies
                           WHERE status='QUALIFIED' AND linkedin_url IS NULL
                           ORDER BY roster_estimate DESC LIMIT 60""")
        found = 0
        for row in rows:
            try:
                url = _find_company_linkedin(ctx, row["canonical_name"])
            except Exception:
                continue
            if url:
                ctx.db.x("""UPDATE companies SET linkedin_url=?, last_researched=?
                            WHERE company_id=?""", (url, utcnow(), row["company_id"]))
                found += 1
        log(f"linkedin-urls  resolved {found}/{len(rows)}")

        # 2. Find people (free LinkedIn-SERP + team pages + role queries).
        log(f"people         {s5_people.run(ctx)}")
        log(f"profiles       {s6_profiles.run(ctx)}")
    finally:
        ctx.close()

    # 3. Upgrade snippet evidence to first-party (cheap, capacity available).
    subprocess.run([sys.executable, "scripts/verify_profiles_batch.py"],
                   cwd=str(ROOT), env=ENV, check=False)

    # 4. Verify, score, export everything -- including anything already cached.
    ctx = Context.create(countries=["GB", "US", "IE", "AU"], db_path="data/master.sqlite")
    try:
        log(f"verify         {s7_person_verify.run(ctx)}")
        log(f"score          {s8_score.run(ctx)}")
        log(f"export         {s9_export.run(ctx)}")
        log(f"run dir        {ctx.run_dir}")
    finally:
        ctx.close()

    subprocess.run([sys.executable, "scripts/export_workbook.py"], cwd=str(ROOT), env=ENV)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
