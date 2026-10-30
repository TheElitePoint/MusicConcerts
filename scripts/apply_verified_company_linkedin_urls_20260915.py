"""Apply manually verified exact LinkedIn company-page mappings.

This script is intentionally narrow and idempotent.  It only fills the four
company URLs verified against both the official company identity and the
LinkedIn company page during the 2026-09-15 run.
"""
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[1]
MAPPINGS = {
    130: "https://www.linkedin.com/company/hospital-records",
    598: "https://www.linkedin.com/company/tru-thoughts",
    2148: "https://www.linkedin.com/company/black-hole-recordings-b.v.",
    2603: "https://www.linkedin.com/company/cooking-vinyl",
}


def main() -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite")
    try:
        for company_id, url in MAPPINGS.items():
            row = con.execute(
                "SELECT canonical_name,status FROM companies WHERE company_id=?",
                (company_id,),
            ).fetchone()
            if not row or row[1] != "QUALIFIED":
                raise RuntimeError(f"Company {company_id} is missing or not qualified")
            con.execute(
                "UPDATE companies SET linkedin_url=? WHERE company_id=?",
                (url, company_id),
            )
            print(f"mapped company_id={company_id} name={row[0]} url={url}")
        con.commit()
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
