"""Add strongest-tier official 4AD activity evidence from its current store."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
URL = "https://shop.4ad.com/new-releases"


def main() -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    try:
        row = con.execute(
            "SELECT company_id FROM companies WHERE canonical_name='4AD' AND status='QUALIFIED'"
        ).fetchone()
        if not row:
            raise SystemExit("qualified 4AD company record not found")
        company_id = int(row[0])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        exists = con.execute(
            "SELECT 1 FROM company_evidence WHERE company_id=? AND kind='activity' AND url=?",
            (company_id, URL),
        ).fetchone()
        if exists:
            print("inserted=0 skipped=1")
            return 0
        con.execute(
            """INSERT INTO company_evidence
               (company_id,kind,url,source,snippet,strength,retrieved_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                company_id,
                "activity",
                URL,
                "official company website",
                "4AD's official new-releases page lists current and upcoming 2026 releases, including dated July and September releases.",
                "strongest",
                now,
            ),
        )
        con.commit()
    finally:
        con.close()
    print("inserted=1 skipped=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
