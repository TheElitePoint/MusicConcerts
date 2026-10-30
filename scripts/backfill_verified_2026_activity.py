"""Store manually verified 2026 activity found during the SOP research pass.

Every row below has a dated, directly resolving evidence URL.  Official company
pages/social profiles are ``strongest`` under SS38; named music-industry reporting
is ``strong``.  The records are intentionally explicit rather than inferred from a
company name or a score deficit.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.pipeline import Context  # noqa: E402


# company_id, category, date, description, URL, source, evidence strength
SIGNALS = [
    (371, "new_signing", "2026-08-27", "Official roster page says HiTech recently signed to Loma Vista Recordings.",
     "https://www.lomavistarecordings.com/", "website", "strongest"),
    (16, "new_signing", "2026-02-03", "Official Napalm release announces that HEATHEN signed with Napalm Records.",
     "https://label.napalmrecords.com/images/bands/heathen/HEATHEN_Never%20A%20God%20Product%20Desription_EN.pdf", "website", "strongest"),
    (159, "new_distribution", "2026-05-21", "Chess Club Records signed a global distribution agreement with Virgin Music Group.",
     "https://creativeindustriesnews.com/2026/05/chess-club-records-picks-umgs-virgin-music-group-for-global-distribution/", "industry_press", "strong"),
    (159, "new_signing", "2026-05-21", "The same 2026 deal report identifies jo from school as a new Chess Club signing.",
     "https://creativeindustriesnews.com/2026/05/chess-club-records-picks-umgs-virgin-music-group-for-global-distribution/", "industry_press", "strong"),
    (169, "new_signing", "2026-08-27", "Current release listing identifies Die Anstalt as recently signed to City Slang worldwide.",
     "https://www.deejay.de/CITY_SLANG__L15888/perpage_20/page_1", "music_retailer", "strong"),
    (34, "new_signing", "2026-04-14", "Family Stereo announced a 2026 signing to Bella Union.",
     "https://www.thelineofbestfit.com/news/family-stereo-signs-to-bella-union-and-announces-debut-album-the-thread", "industry_press", "strong"),
    (66, "new_signing", "2026-01-15", "Modern Woman signed to One Little Independent Records for its 2026 debut album.",
     "https://www.recordoftheday.com/on-the-move/news-press/modern-women-have-signed-to-one-little-independent-records", "industry_press", "strong"),
    (324, "new_signing", "2026-05-22", "Frontiers' official page announces Dan Byrne's signing and debut album.",
     "https://www.frontiers.it/album/6090", "website", "strongest"),
    (409, "new_signing", "2026-08-22", "Babe Haven signed to Hassle Records for an October 2026 release.",
     "https://mcgigmusic.com/babe-haven-sign-to-hassle-records-set-to-release-the-album-nuisance/", "industry_press", "strong"),
    (330, "market_expansion", "2026-05-21", "Third Man Records launched the Third Man Release Lab in 2026.",
     "https://thirdmanrecords.com/blogs/news", "website", "strongest"),
    (1841, "market_expansion", "2026-03-09", "C9 Entertainment launched NAZE, its first boy group in five years, for a May 2026 debut.",
     "https://hitkultr.com/brands/c9-entertainment", "music_press", "strong"),
]


# Current official release/activity pages.  These affect D2 evidence quality only;
# they do not manufacture an additional activity category.
OFFICIAL_ACTIVITY = [
    (246, "https://www.thirtytigers.com/releases/2022", "Official release page lists multiple February 2026 releases."),
    (371, "https://www.lomavistarecordings.com/", "Official page lists current 2026 videos, releases and the recent HiTech signing."),
    (16, "https://label.napalmrecords.com/images/bands/heathen/HEATHEN_Never%20A%20God%20Product%20Desription_EN.pdf", "Official signing and 2026 release announcement."),
    (159, "https://chessclub-records.co.uk/pages/music", "Official store lists current 2026 releases."),
    (169, "https://shop-us.cityslang.com/collections/vinyl", "Official label shop lists the current catalogue and preorders."),
    (34, "https://bellaunion.com/products/congratulations-join-hands", "Official label page for a February 2026 release."),
    (66, "https://olirecords.bandcamp.com/", "Official label store lists multiple 2026 releases including Modern Woman."),
    (324, "https://www.frontiers.it/album/6090", "Official signing page and May 2026 release."),
    (409, "https://linktr.ee/hasslerecords", "Official Hassle profile lists Babe Haven and current releases."),
    (330, "https://thirdmanrecords.com/blogs/news", "Official news page lists multiple dated 2026 initiatives and releases."),
    (103, "https://deadoceans.com/releases/", "Official discography lists multiple 2026 releases and preorders."),
]


def main() -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        inserted_signals = inserted_evidence = 0
        for cid, category, observed, desc, url, source, strength in SIGNALS:
            cur = ctx.db.x(
                """INSERT OR IGNORE INTO activity_signals
                   (company_id, raw_label, signal_type, description, evidence_url,
                    observed_at, recorded_at, source, external_id)
                   VALUES (?,(SELECT canonical_name FROM companies WHERE company_id=?),
                           ?,?,?,?,?,?,?)""",
                (cid, cid, category, desc, url, observed, utcnow(), source,
                 f"verified-2026:{cid}:{category}:{url}"),
            )
            inserted_signals += int(bool(cur.rowcount))
            if strength == "strongest":
                cur = ctx.db.x(
                    """INSERT INTO company_evidence
                       (company_id, kind, url, source, snippet, strength, retrieved_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(company_id,kind,url) DO UPDATE SET
                         source=excluded.source, snippet=excluded.snippet,
                         strength=excluded.strength, retrieved_at=excluded.retrieved_at""",
                    (cid, "activity", url, source, desc, strength, utcnow()),
                )
                inserted_evidence += int(bool(cur.rowcount))

        for cid, url, snippet in OFFICIAL_ACTIVITY:
            cur = ctx.db.x(
                """INSERT INTO company_evidence
                   (company_id, kind, url, source, snippet, strength, retrieved_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(company_id,kind,url) DO UPDATE SET
                     source=excluded.source, snippet=excluded.snippet,
                     strength=excluded.strength, retrieved_at=excluded.retrieved_at""",
                (cid, "activity", url, "website", snippet, "strongest", utcnow()),
            )
            inserted_evidence += int(bool(cur.rowcount))

        print(f"activity_signals_inserted={inserted_signals}")
        print(f"official_activity_evidence_upserted={inserted_evidence}")
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
