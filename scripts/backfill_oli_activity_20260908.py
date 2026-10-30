"""Add dated 2026 One Little Independent Records release signals."""
from __future__ import annotations

import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SIGNALS = [
    (
        "official company website",
        "oli-delilah-holliday-fat-cat-2026-08-28",
        "2026-08-28",
        "https://olirecords.com/products/delilah-holliday-fat-cat",
        "Delilah Holliday released Fat Cat via One Little Independent Records on 2026-08-28.",
    ),
    (
        "Spotify",
        "oli-kaktus-einarsson-factoid-happiness-2026-09-18",
        "2026-09-18",
        "https://open.spotify.com/album/2GThrLLl8X1TUHE4owuALW",
        "Kaktus Einarsson's Factoid Happiness is scheduled via One Little Independent Records for 2026-09-18.",
    ),
    (
        "MusicBrainz",
        "oli-asgeir-julia-deluxe-2026-09-09",
        "2026-09-09",
        "https://musicbrainz.org/release/c9f967b0-024b-4663-bcda-167254693535",
        "Ásgeir's Julia (Deluxe) is scheduled via One Little Independent Records for 2026-09-09.",
    ),
]


def main() -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    try:
        row = con.execute(
            """SELECT company_id FROM companies
               WHERE canonical_name='One Little Independent Records' AND status='QUALIFIED'"""
        ).fetchone()
        if not row:
            raise SystemExit("qualified One Little Independent Records record not found")
        company_id = int(row[0])
        inserted = 0
        for source, external_id, observed_at, url, description in SIGNALS:
            cur = con.execute(
                """INSERT OR IGNORE INTO activity_signals
                   (company_id,raw_label,signal_type,description,evidence_url,
                    observed_at,recorded_at,source,external_id)
                   VALUES (?,?,?,?,?,?,datetime('now'),?,?)""",
                (
                    company_id,
                    "One Little Independent Records",
                    "release_activity",
                    description,
                    url,
                    observed_at,
                    source,
                    external_id,
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
