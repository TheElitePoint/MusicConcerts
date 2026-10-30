"""Correct Jacob Round's employer from Noise Records to Pure Noise Records."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    try:
        company = con.execute(
            "SELECT company_id FROM companies WHERE canonical_name='Pure Noise Records' AND status='QUALIFIED'"
        ).fetchone()
        if not company:
            raise SystemExit("qualified Pure Noise Records company not found")
        company_id = int(company[0])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = con.execute(
            "UPDATE people SET company_id=? WHERE person_id=2338",
            (company_id,),
        )
        con.execute(
            """UPDATE scores SET why_company=? WHERE person_id=2338""",
            (
                "Pure Noise Records is a qualified independent record label with a focused multi-artist roster and current release activity.",
            ),
        )
        con.execute(
            """INSERT OR IGNORE INTO company_evidence
               (company_id,kind,url,source,snippet,strength,retrieved_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                company_id,
                "activity",
                "https://www.purenoise.net/",
                "official company website",
                "Official site lists September 2026 releases and current label signings.",
                "strongest",
                now,
            ),
        )
        for signal_type, description, url, observed_at, external_id in (
            (
                "release_activity",
                "156/Silence released From A Distance via Pure Noise Records on 2026-09-04.",
                "https://www.purenoise.net/",
                "2026-09-04",
                "pure-noise-156-silence-from-a-distance-2026-09-04",
            ),
            (
                "new_signing",
                "Pure Noise Records announced the signing of Deux Visages on 2026-06-23.",
                "https://www.purenoise.net/news/",
                "2026-06-23",
                "pure-noise-deux-visages-signing-2026-06-23",
            ),
        ):
            con.execute(
                """INSERT OR IGNORE INTO activity_signals
                   (company_id,raw_label,signal_type,description,evidence_url,
                    observed_at,recorded_at,source,external_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    company_id,
                    "Pure Noise Records",
                    signal_type,
                    description,
                    url,
                    observed_at,
                    now,
                    "official company website",
                    external_id,
                ),
            )
        con.commit()
    finally:
        con.close()
    print(f"corrected={cur.rowcount} company_id={company_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
