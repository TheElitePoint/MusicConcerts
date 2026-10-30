"""Add current, dated Thirty Tigers release evidence found in the public web pass.

These records are company-level SOP activity signals.  They do not alter role
classification, employment verification, or scoring thresholds.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SIGNALS = [
    {
        "source": "Shore Fire Media",
        "external_id": "thirty-tigers-alicia-witt-between-heaven-and-earth-2026-08-28",
        "observed_at": "2026-08-28",
        "url": "https://shorefire.com/releases/entry/alicia-witt-releases-new-albumbetween-heaven-and-earthtoday-via-thirty-tigers",
        "description": "Alicia Witt released Between Heaven and Earth via Thirty Tigers on 2026-08-28.",
    },
    {
        "source": "Apple Music",
        "external_id": "thirty-tigers-steel-woods-lexington-tapes-2026-08-21",
        "observed_at": "2026-08-21",
        "url": "https://music.apple.com/us/album/the-lexington-tapes-live/6767329081",
        "description": "The Steel Woods released The Lexington Tapes, marketed and distributed by Thirty Tigers, on 2026-08-21.",
    },
]


def main() -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    try:
        row = con.execute(
            "SELECT company_id FROM companies WHERE canonical_name=? AND status='QUALIFIED'",
            ("Thirty Tigers",),
        ).fetchone()
        if not row:
            raise SystemExit("qualified Thirty Tigers company record not found")
        company_id = int(row[0])
        inserted = 0
        for signal in SIGNALS:
            cur = con.execute(
                """INSERT OR IGNORE INTO activity_signals
                   (company_id,raw_label,signal_type,description,evidence_url,
                    observed_at,recorded_at,source,external_id)
                   VALUES (?,?,?,?,?,?,datetime('now'),?,?)""",
                (
                    company_id,
                    "Thirty Tigers",
                    "release_activity",
                    signal["description"],
                    signal["url"],
                    signal["observed_at"],
                    signal["source"],
                    signal["external_id"],
                ),
            )
            inserted += cur.rowcount
        con.commit()
    finally:
        con.close()
    print(f"inserted={inserted} skipped={len(SIGNALS) - inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
