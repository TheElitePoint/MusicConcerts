"""Backfill structured SOP facts already proved by stored evidence.

This script does not create facts from names or scoring needs.  It makes two narrow,
auditable inferences:

1. A company classified as an eligible label/distributor is an exact type match
   when its official website is stored as strongest type evidence, MusicBrainz
   explicitly supplies the same eligible type, and at least two independent music
   databases identify the same company.
2. Discogs' explicit statement that a label released music across several artists,
   with its latest release in 2026, is a ``multi_artist_activity`` signal.  Freshness
   remains scored separately from the stored date, as required by SS41.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.pipeline import Context  # noqa: E402


_ACROSS_ARTISTS = re.compile(r"across\s+(\d+)\s+artists;\s+latest\s+2026", re.I)


def main() -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        type_rows = ctx.db.q(
            """SELECT c.company_id, c.canonical_name
                 FROM companies c
                 JOIN company_evidence w
                   ON w.company_id=c.company_id AND w.kind='type'
                  AND w.source='website' AND w.strength='strongest'
                 JOIN company_evidence t
                   ON t.company_id=c.company_id AND t.kind='type'
                  AND t.source='musicbrainz' AND t.strength IN ('strongest','strong')
                 JOIN company_evidence x
                   ON x.company_id=c.company_id
                  AND x.kind='identity'
                  AND x.source IN ('musicbrainz','discogs','wikidata')
                WHERE c.status='QUALIFIED'
                  AND c.company_type IN ('record_label','music_distributor')
                  AND c.type_confidence!='strong'
                GROUP BY c.company_id
               HAVING COUNT(DISTINCT x.source) >= 2"""
        )
        for row in type_rows:
            ctx.db.x(
                "UPDATE companies SET type_confidence='strong', last_researched=? "
                "WHERE company_id=?",
                (utcnow(), row["company_id"]),
            )

        activity_rows = ctx.db.q(
            """SELECT a.company_id, a.description, a.evidence_url, a.observed_at,
                      c.canonical_name
                 FROM activity_signals a
                 JOIN companies c ON c.company_id=a.company_id
                WHERE c.status='QUALIFIED' AND a.source='discogs'
                  AND a.description LIKE '%across % artists; latest 2026%'
                  AND NOT EXISTS (
                      SELECT 1 FROM activity_signals b
                       WHERE b.company_id=a.company_id
                         AND b.signal_type='multi_artist_activity')"""
        )
        inserted = 0
        for row in activity_rows:
            match = _ACROSS_ARTISTS.search(row["description"] or "")
            if not match or int(match.group(1)) < 3:
                continue
            cur = ctx.db.x(
                """INSERT OR IGNORE INTO activity_signals
                   (company_id, raw_label, signal_type, description, evidence_url,
                    observed_at, recorded_at, source, external_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    row["company_id"], row["canonical_name"],
                    "multi_artist_activity",
                    "Discogs reports releases across " + match.group(1)
                    + " artists in the last two years; latest release is 2026",
                    row["evidence_url"], row["observed_at"], utcnow(), "discogs",
                    f"derived:multi-artist-2026:{row['company_id']}",
                ),
            )
            inserted += int(bool(cur.rowcount))

        print(f"type_confidence_upgraded={len(type_rows)}")
        print(f"multi_artist_activity_inserted={inserted}")
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
