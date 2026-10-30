"""Store narrowly verified first-party and industry evidence for the additional LinkedIn run.

Every row below comes from an official company/platform page or a named industry
publication that explicitly supports a current 2025/2026 activity or employee/title.
This script does not infer facts from names and does not alter qualification thresholds.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.pipeline import Context  # noqa: E402


ACTIVITY = [
    (
        "FABTONE",
        "https://www.fabtone.co.jp/release/",
        "Official FABTONE release catalogue lists multiple specifically dated 2026 releases.",
    ),
    (
        "Big Loud Records",
        "https://bigloud.com/stephen-wilson-jr-wins-his-first-acm-award/",
        "Official Big Loud page dated 2026-05-07 documents current artist campaign activity and names label staff.",
    ),
    (
        "Easy Star Records",
        "https://easystar.com/releases/",
        "Official release catalogue lists multiple Easy Star releases dated 2026.",
    ),
    (
        "Hollywood Records",
        "https://www.hollywoodrecords.com/press/hudson-stone/",
        "Official Hollywood Records press page documents a 2026 signing and debut release campaign.",
    ),
    (
        "Whirlwind Recordings",
        "https://www.whirlwindrecordings.com/category/news/",
        "Official label news archive lists multiple dated 2026 releases and a 2026 signing.",
    ),
    (
        "Lofi Girl",
        "https://link.lofigirl.com/m/linkinbio-lofigirlshop",
        "Official Lofi Girl subdomain lists the Spring and Summer Collection 2026 vinyl releases.",
    ),
    (
        "avex entertainment",
        "https://avexnet.jp/contents/WFHLH-OFCL-0001/news/1135461",
        "Official Avex portal documents the 2026-08-19 release of a new single.",
    ),
    (
        "YEAR0001",
        "https://year0001.com/label",
        "Official YEAR0001 label page lists its current multi-artist release catalogue.",
    ),
    (
        "SO Recordings",
        "https://linktr.ee/so.recordings",
        "Official SO Recordings profile links its current releases and campaigns.",
    ),
    (
        "CUBE Entertainment",
        "https://cubeent.co.jp/",
        "Official CUBE Entertainment Japan page lists multiple dated 2026 releases from CUBE artists.",
    ),
    (
        "Dreamus",
        "https://www.dreamuscompany.com/notice/r_view/0/944",
        "Official Dreamus page dated 2026-04-20 documents expansion of its artist-IP investment, distribution and partnership work.",
    ),
    (
        "Hardly Art",
        "https://www.hardlyart.com/blogs/news",
        "Official Hardly Art news page lists multiple label releases dated March-May 2026.",
    ),
    (
        "Neon Gold",
        "https://www.neongoldrecords.com/releases/",
        "Official Neon Gold page lists its current multi-artist release catalogue.",
    ),
    (
        "YB Music",
        "https://music.apple.com/us/album/falar-pouco-single/1883060465",
        "Apple Music lists Falar Pouco by Livia Nestrovski and Luca Raele as a 2026 YB Music release.",
    ),
]


SIGNALS = [
    (
        "FABTONE", "release_activity", "Official FABTONE catalogue documents the 2026-07-08 release of Meiou to Jiai no Tenshi.",
        "https://www.fabtone.co.jp/release/907/", "2026-07-08", "website", "verified-2026:1495:release:907",
    ),
    (
        "Third Man Records", "release_activity", "Jack White announced a new studio album through Third Man Records.",
        "https://thirdmanrecords.com/blogs/news", "2026-06-10", "website", "verified-2026:330:release:2026-06-10",
    ),
    (
        "avex entertainment", "release_activity", "Official Avex portal documents a new single released on 2026-08-19.",
        "https://avexnet.jp/contents/WFHLH-OFCL-0001/news/1135461", "2026-08-19", "website", "verified-2026:223:release:2026-08-19",
    ),
    (
        "Hollywood Records", "new_signing", "Official Hollywood Records press page announces Hudson Stone's exclusive recording contract.",
        "https://www.hollywoodrecords.com/press/hudson-stone/", "2026-03-31", "website", "verified-2026:569:signing:hudson-stone",
    ),
    (
        "Hardly Art", "release_activity", "Official Hardly Art page documents Marinero's 2026-04-01 label release.",
        "https://www.hardlyart.com/blogs/news/marinero-releases-la-la-la-remixes", "2026-04-01", "website", "verified-2026:249:release:2026-04-01",
    ),
    (
        "Dreamus", "partnership", "Official Dreamus page documents expansion of its artist-IP investment and partnership model.",
        "https://www.dreamuscompany.com/notice/r_view/0/944", "2026-04-20", "website", "verified-2026:1833:partnership:2026-04-20",
    ),
    (
        "Neon Gold", "release_activity", "Apple Music lists TEHYA's catch//release single as a 2026 Neon Gold Records release.",
        "https://music.apple.com/be/album/catch-release-single/1896034914", "2026-06-17", "apple_music", "verified-2026:310:release:catch-release",
    ),
    (
        "YB Music", "release_activity", "Apple Music lists Falar Pouco by Livia Nestrovski and Luca Raele as a 2026 YB Music release.",
        "https://music.apple.com/us/album/falar-pouco-single/1883060465", "2026-04-02", "apple_music", "verified-2026:1648:release:falar-pouco",
    ),
]


TYPE_EVIDENCE = [
    (
        "Earache Records",
        "https://earache.com/pages/about-us",
        "Official Earache About Us page explicitly describes Earache Records as a record label that signs and promotes a diverse artist roster.",
    ),
]


EMPLOYMENT = [
    (
        1410,
        "https://bigloud.com/big-loud-records-bolsters-executive-marketing-team-with-new-hires-and-key-promotions/",
        "Official Big Loud announcement dated 2025-08-18 names Amy Beard as VP of Marketing.",
    ),
    (
        1418,
        "https://bigloud.com/big-loud-records-bolsters-executive-marketing-team-with-new-hires-and-key-promotions/",
        "Official Big Loud announcement dated 2025-08-18 names Brianne Deslippe as SVP of Marketing.",
    ),
    (
        1412,
        "https://bigloud.com/coleman-jennings-signs-with-big-loud-texas-and-mercury-records/",
        "Official Big Loud announcement dated 2025-09-19 names Sara Knabe as SVP of A&R.",
    ),
    (
        1421,
        "https://bigloud.com/ashley-cooke-stacks-star-studded-week-of-performances-in-nashville-with-help-from-joe-jonas-jordan-davis-nate-smith-and-more/",
        "Official Big Loud announcement dated 2025-02-24 names Hillary Lambert as Director of Marketing.",
    ),
    (
        1411,
        "https://bigloud.com/stephen-wilson-jr-wins-his-first-acm-award/",
        "Official Big Loud page dated 2026-05-07 identifies Nate Yetton with Big Loud Records; the verified LinkedIn profile supplies his current VP of A&R title.",
    ),
    (
        1414,
        "https://countrycredits.com/cma-announces-new-class-of-womens-leadership-academy/",
        "Current music-industry announcement identifies Jamie Lynn Younger as Vice President, Marketing at Big Loud.",
    ),
]


def main() -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    activity_added = signal_added = employment_added = type_added = 0
    try:
        for company, url, snippet in TYPE_EVIDENCE:
            row = ctx.db.one(
                "SELECT company_id FROM companies WHERE canonical_name=? AND status='QUALIFIED'",
                (company,),
            )
            if not row:
                continue
            cur = ctx.db.x(
                """INSERT OR IGNORE INTO company_evidence
                   (company_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (row["company_id"], "type", url, "website", snippet, "strongest", utcnow()),
            )
            type_added += int(bool(cur.rowcount))
            ctx.db.x(
                """UPDATE companies SET company_type='record_label',type_confidence='strong',
                   last_researched=? WHERE company_id=?""",
                (utcnow(), row["company_id"]),
            )

        for company, url, snippet in ACTIVITY:
            row = ctx.db.one(
                "SELECT company_id FROM companies WHERE canonical_name=? AND status='QUALIFIED'",
                (company,),
            )
            if not row:
                continue
            evidence_source = "industry_press" if "countrycredits.com" in url else "website"
            cur = ctx.db.x(
                """INSERT OR IGNORE INTO company_evidence
                   (company_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (row["company_id"], "activity", url, "website", snippet, "strongest", utcnow()),
            )
            activity_added += int(bool(cur.rowcount))

        for company, signal_type, description, url, observed_at, source, external_id in SIGNALS:
            row = ctx.db.one(
                "SELECT company_id FROM companies WHERE canonical_name=? AND status='QUALIFIED'",
                (company,),
            )
            if not row:
                continue
            cur = ctx.db.x(
                """INSERT OR IGNORE INTO activity_signals
                   (company_id,raw_label,signal_type,description,evidence_url,
                    observed_at,recorded_at,source,external_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (row["company_id"], company, signal_type, description, url,
                 observed_at, utcnow(), source, external_id),
            )
            signal_added += int(bool(cur.rowcount))

        for person_id, url, snippet in EMPLOYMENT:
            row = ctx.db.one(
                """SELECT p.person_id FROM people p JOIN companies c USING(company_id)
                    WHERE p.person_id=? AND p.status!='REJECTED'
                      AND c.canonical_name='Big Loud Records'""",
                (person_id,),
            )
            if not row:
                continue
            cur = ctx.db.x(
                """INSERT OR IGNORE INTO person_evidence
                   (person_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (person_id, "employment", url, evidence_source, snippet, "strongest", utcnow()),
            )
            employment_added += int(bool(cur.rowcount))
            ctx.db.x(
                "UPDATE people SET employment_sources=2,last_researched=? WHERE person_id=?",
                (utcnow(), person_id),
            )

        print(f"activity_evidence_added={activity_added}")
        print(f"activity_signals_added={signal_added}")
        print(f"employment_evidence_added={employment_added}")
        print(f"type_evidence_added={type_added}")
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
