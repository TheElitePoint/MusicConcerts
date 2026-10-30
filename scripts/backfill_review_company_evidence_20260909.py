"""Backfill official company evidence for verified Instagram review cases."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    inserted_evidence = 0
    inserted_signals = 0
    try:
        companies = {
            row[1]: int(row[0])
            for row in con.execute(
                "SELECT company_id,canonical_name FROM companies WHERE canonical_name IN ('8ball Music','Defected','Intrada')"
            )
        }

        evidence = [
            (
                "8ball Music",
                "activity",
                "https://www.8ballmusic.nl/",
                "Official site presents the current artist roster and releases section.",
            ),
            (
                "Defected",
                "activity",
                "https://defected.com/",
                "Official site lists dated August and September 2026 releases and partnership news.",
            ),
            (
                "Intrada",
                "activity",
                "https://store.intrada.com/s.nl/sc.13/category.21780/.f",
                "Official coming-soon catalog lists multiple July 2026 Intrada releases.",
            ),
        ]
        for company, kind, url, snippet in evidence:
            inserted_evidence += con.execute(
                """INSERT OR IGNORE INTO company_evidence
                   (company_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (companies[company], kind, url, "official company website", snippet, "strongest", now),
            ).rowcount

        # The official sites explicitly identify 8ball as a record company and
        # Intrada as an independent label, resolving earlier moderate typing.
        for company in ("8ball Music", "Intrada"):
            con.execute(
                "UPDATE companies SET type_confidence='strong' WHERE company_id=? AND company_type='record_label'",
                (companies[company],),
            )

        signals = [
            (
                "Defected",
                "partnership",
                "Defected partnered with Aston Martin for a three-day Soho takeover announced 2026-08-25.",
                "https://defected.com/news/post/defected-partners-with-aston-martin-to-launch-three-day-takeover-in-soho-london/",
                "2026-08-25",
                "defected-aston-martin-partnership-2026-08-25",
            ),
            (
                "Defected",
                "release_activity",
                "LEYO & Thierry Ganz released Stamina via Defected on 2026-09-04.",
                "https://defected.com/",
                "2026-09-04",
                "defected-stamina-2026-09-04",
            ),
            (
                "Defected",
                "release_activity",
                "Melé & RIA released Ay Sí via Defected on 2026-08-28.",
                "https://defected.com/",
                "2026-08-28",
                "defected-ay-si-2026-08-28",
            ),
            (
                "Defected",
                "release_activity",
                "The Shapeshifters featuring 79.5 released On The Way Up via Defected on 2026-08-21.",
                "https://defected.com/",
                "2026-08-21",
                "defected-on-the-way-up-2026-08-21",
            ),
            (
                "Intrada",
                "release_activity",
                "Intrada announced Coming to America for July 2026.",
                "https://store.intrada.com/s.nl/sc.13/category.21780/.f",
                "2026-07-01",
                "intrada-coming-to-america-2026-07",
            ),
            (
                "Intrada",
                "release_activity",
                "Intrada announced Bittersweet: Music for Dark Comedies for July 2026.",
                "https://store.intrada.com/s.nl/sc.13/category.21780/.f",
                "2026-07-01",
                "intrada-bittersweet-2026-07",
            ),
            (
                "Intrada",
                "release_activity",
                "Intrada announced Felice... Felice... / Iris for July 2026.",
                "https://store.intrada.com/s.nl/sc.13/category.21780/.f",
                "2026-07-01",
                "intrada-felice-iris-2026-07",
            ),
        ]
        for company, signal_type, description, url, observed_at, external_id in signals:
            inserted_signals += con.execute(
                """INSERT OR IGNORE INTO activity_signals
                   (company_id,raw_label,signal_type,description,evidence_url,
                    observed_at,recorded_at,source,external_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    companies[company],
                    company,
                    signal_type,
                    description,
                    url,
                    observed_at,
                    now,
                    "official company website",
                    external_id,
                ),
            ).rowcount
        con.commit()
    finally:
        con.close()
    print(f"evidence_inserted={inserted_evidence} signals_inserted={inserted_signals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
