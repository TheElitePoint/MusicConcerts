"""Add current official Far Out Recordings activity evidence.

This is a narrow SOP evidence correction: the official label site lists a
dated 2026 release, while the stored company score still used stale activity.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.faroutrecordings.com/release/586862-moyses-dos-santos-maria"


def main() -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    try:
        row = con.execute(
            "SELECT company_id FROM companies WHERE canonical_name=? AND status='QUALIFIED'",
            ("Far Out Recordings",),
        ).fetchone()
        if not row:
            raise SystemExit("qualified Far Out Recordings record not found")
        company_id = int(row[0])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        activity = con.execute(
            """INSERT OR IGNORE INTO activity_signals
               (company_id,raw_label,signal_type,description,evidence_url,
                observed_at,recorded_at,source,external_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                company_id,
                "Far Out Recordings",
                "release_activity",
                "Moyses Dos Santos released Maria via Far Out Recordings on 2026-06-12.",
                URL,
                "2026-06-12",
                now,
                "official company website",
                "far-out-moyses-dos-santos-maria-2026-06-12",
            ),
        ).rowcount
        evidence = con.execute(
            """INSERT OR IGNORE INTO company_evidence
               (company_id,kind,url,source,snippet,strength,retrieved_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                company_id,
                "activity",
                URL,
                "official company website",
                "Official release page for Moyses Dos Santos' Maria, released 2026-06-12 via Far Out Recordings.",
                "strongest",
                now,
            ),
        ).rowcount
        type_evidence = con.execute(
            """INSERT OR IGNORE INTO company_evidence
               (company_id,kind,url,source,snippet,strength,retrieved_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                company_id,
                "type",
                URL,
                "official company website",
                "The official product page identifies Far Out Recordings as the release label.",
                "strongest",
                now,
            ),
        ).rowcount
        con.execute(
            "UPDATE companies SET type_confidence='strong' WHERE company_id=? AND company_type='record_label'",
            (company_id,),
        )
        con.commit()
    finally:
        con.close()
    print(
        f"activity_inserted={activity} evidence_inserted={evidence} "
        f"type_evidence_inserted={type_evidence}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
