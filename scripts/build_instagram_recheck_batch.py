"""Export existing unverified Instagram profiles for a fresh Apify recheck."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/instagram_runs/existing_unverified_recheck_20260911.json"


def main() -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT p.person_id,pr.handle,pr.url
             FROM people p JOIN profiles pr USING(person_id)
             JOIN companies c USING(company_id)
            WHERE pr.channel='instagram'
              AND pr.verification_status='UNVERIFIED'
              AND p.status!='REJECTED' AND p.current_verified=1
              AND c.status='QUALIFIED'
            ORDER BY p.person_id"""
    ).fetchall()
    con.close()
    payload = [
        {"person_id": int(row["person_id"]),
         "candidates": [{"handle": row["handle"], "url": row["url"]}]}
        for row in rows if row["handle"]
    ]
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"rows={len(payload)} output={OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
