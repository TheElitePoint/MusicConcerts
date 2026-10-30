"""Ingest FALA label and correct Bartlomiej Kopiniak's current owner role.

Evidence was checked against FALA's official site, its official Instagram, the
public personal Instagram profile, Spotify release metadata, and Poland's company
registry mirror.  This script is intentionally limited to one audited company and
one already-known person identity.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/master.sqlite"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    stamp = now()
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    try:
        company = con.execute(
            "SELECT company_id FROM companies WHERE domain='falalabel.pl' OR canonical_name='FALA label'"
        ).fetchone()
        if company:
            company_id = int(company["company_id"])
            con.execute(
                """UPDATE companies SET canonical_name='FALA label',name_normalized='fala label',
                       name_loose='fala',domain='falalabel.pl',website='https://www.falalabel.pl/',
                       instagram_url='https://www.instagram.com/fala.label/',country='PL',
                       country_confidence='verified',company_type='record_label',
                       type_confidence='strong',roster_estimate=4,roster_low=4,roster_high=4,
                       roster_display='4 artists',
                       roster_evidence_url='https://www.instagram.com/fala.label/',
                       description='Independent Polish record label representing and releasing music for multiple artists.',
                       status='QUALIFIED',gate_result=?,last_researched=?
                     WHERE company_id=?""",
                (json.dumps({f"G{i}": "PASS" for i in range(1, 9)}, sort_keys=True), stamp, company_id),
            )
        else:
            cur = con.execute(
                """INSERT INTO companies
                   (canonical_name,name_normalized,name_loose,domain,website,instagram_url,
                    country,country_confidence,company_type,type_confidence,roster_estimate,
                    roster_low,roster_high,roster_display,roster_evidence_url,description,
                    status,gate_result,first_seen,last_researched,discovered_via)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "FALA label", "fala label", "fala", "falalabel.pl",
                    "https://www.falalabel.pl/", "https://www.instagram.com/fala.label/",
                    "PL", "verified", "record_label", "strong", 4, 4, 4, "4 artists",
                    "https://www.instagram.com/fala.label/",
                    "Independent Polish record label representing and releasing music for multiple artists.",
                    "QUALIFIED", json.dumps({f"G{i}": "PASS" for i in range(1, 9)}, sort_keys=True),
                    stamp, stamp, "signed_in_instagram_and_official_site",
                ),
            )
            company_id = int(cur.lastrowid)

        con.execute(
            """INSERT OR IGNORE INTO company_aliases
               (alias_normalized,company_id,alias_raw,source,created_at)
               VALUES ('fala label',?,'FALA label','official company website',?)""",
            (company_id, stamp),
        )

        evidence = (
            ("identity", "https://www.falalabel.pl/", "official company website",
             "Official site identifies the business as FALA label and uses the falalabel.pl domain.", "strongest"),
            ("type", "https://www.falalabel.pl/", "official company website",
             "Official label site has an ARTYŚCI section and artist pages for a multi-artist record label.", "strongest"),
            ("roster", "https://www.instagram.com/fala.label/", "official company instagram",
             "Official profile identifies four current artists: Briela, maks.tachasiuk, Rembikowski and VHS.", "strongest"),
            ("roster", "https://www.falalabel.pl/", "official company website",
             "Official site lists multiple current artists including VHS, maks.tachasiuk and Briela.", "strongest"),
            ("country", "https://www.falalabel.pl/", "official company website",
             "Polish-language official site on the .pl domain supports Poland as the primary market.", "strong"),
            ("country", "https://rejestr.io/krs/1031787/fala-label", "Polish company registry mirror",
             "FALA LABEL sp. z o.o. is registered at Nowy Świat 33/13, Warsaw, Poland.", "strongest"),
            ("activity", "https://www.falalabel.pl/maks-tachasiuk/", "official company website",
             "Official artist page documents the 17 April 2026 FALA label album release and an active 2026 concert campaign through September.", "strongest"),
        )
        for kind, url, source, snippet, strength in evidence:
            con.execute(
                """INSERT INTO company_evidence
                   (company_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(company_id,kind,url) DO UPDATE SET
                     source=excluded.source,snippet=excluded.snippet,
                     strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                (company_id, kind, url, source, snippet, strength, stamp),
            )

        artists = (
            ("Briela", "briela", "https://www.falalabel.pl/briela/"),
            ("maks.tachasiuk", "maks tachasiuk", "https://www.falalabel.pl/maks-tachasiuk/"),
            ("Rembikowski", "rembikowski", "https://www.instagram.com/fala.label/"),
            ("VHS", "vhs", "https://www.falalabel.pl/vhs/"),
        )
        for name, normalized, url in artists:
            con.execute(
                """INSERT OR IGNORE INTO artists
                   (company_id,name,name_normalized,relationship,evidence_url,recorded_at)
                   VALUES (?,?,?,?,?,?)""",
                (company_id, name, normalized, "label", url, stamp),
            )

        signals = (
            ("release_activity", "maks.tachasiuk album Wieje released by FALA label on 17 April 2026.",
             "https://www.falalabel.pl/maks-tachasiuk/", "2026-04-17", "official company website", "fala-wieje-2026-04-17"),
            ("release_activity", "maks.tachasiuk single Muzyka pop released by FALA label on 27 March 2026.",
             "https://open.spotify.com/album/0xLwcioIso3I9TpCYinABA", "2026-03-27", "Spotify", "fala-muzyka-pop-2026-03-27"),
            ("active_campaign", "FALA artist maks.tachasiuk performed the Erste Letnie Brzmienia campaign date in Poznań.",
             "https://www.falalabel.pl/maks-tachasiuk/", "2026-09-04", "official company website", "fala-maks-tour-2026-09-04"),
            ("active_campaign", "FALA artist maks.tachasiuk performed the Męskie Granie campaign date in Warsaw.",
             "https://www.falalabel.pl/maks-tachasiuk/", "2026-08-22", "official company website", "fala-maks-tour-2026-08-22"),
            ("active_campaign", "FALA artist maks.tachasiuk performed the Bitter Sweet Festival campaign date in Poznań.",
             "https://www.falalabel.pl/maks-tachasiuk/", "2026-08-14", "official company website", "fala-maks-tour-2026-08-14"),
        )
        for signal_type, description, url, observed, source, external_id in signals:
            con.execute(
                """INSERT OR IGNORE INTO activity_signals
                   (company_id,raw_label,signal_type,description,evidence_url,observed_at,
                    recorded_at,source,external_id) VALUES (?,?,?,?,?,?,?,?,?)""",
                (company_id, "FALA label", signal_type, description, url, observed, stamp, source, external_id),
            )

        con.execute(
            """UPDATE people SET company_id=?,title='Owner & Artist Manager',
                   title_family='leadership',seniority='executive',role_relevance=10,
                   current_verified=1,employment_sources=3,status='VERIFIED',
                   last_researched=? WHERE person_id=587""",
            (company_id, stamp),
        )
        person_evidence = (
            ("employment", "https://www.instagram.com/bartek.kopiniak/", "instagram signed-in profile",
             "Public profile bio says owner and artist manager at @fala.label.", "strongest"),
            ("title", "https://www.instagram.com/bartek.kopiniak/", "instagram signed-in profile",
             "Public profile explicitly states owner and artist manager.", "strongest"),
            ("employment", "https://www.falalabel.pl/maks-tachasiuk/", "official company website",
             "Official FALA artist page lists BARTEK KOPINIAK as a company contact.", "strongest"),
            ("employment", "https://rejestr.io/krs/1031787/fala-label", "Polish company registry mirror",
             "Registry record identifies Bartłomiej Kopiniak as a board member and shareholder of FALA LABEL since 2023.", "strongest"),
        )
        for kind, url, source, snippet, strength in person_evidence:
            con.execute(
                """INSERT INTO person_evidence
                   (person_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (587,?,?,?,?,?,?)
                   ON CONFLICT(person_id,kind,url) DO UPDATE SET
                     source=excluded.source,snippet=excluded.snippet,
                     strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                (kind, url, source, snippet, strength, stamp),
            )

        signals_json = json.dumps({
            "company_in_bio": True,
            "role_in_bio": True,
            "external_links_confirm": True,
            "matches_linkedin_identity": False,
            "username_resembles_name": False,
        }, sort_keys=True)
        con.execute(
            """INSERT INTO profiles
               (person_id,channel,url,handle,verification_status,identity_score,
                usability,match_signals,verified_at,retrieved_at)
               VALUES (587,'instagram','https://www.instagram.com/bartek.kopiniak/',
                       'bartek.kopiniak','VERIFIED',8,'full',?,?,?)
               ON CONFLICT(person_id,channel) DO UPDATE SET
                 url=excluded.url,handle=excluded.handle,
                 verification_status=excluded.verification_status,
                 identity_score=excluded.identity_score,usability=excluded.usability,
                 match_signals=excluded.match_signals,verified_at=excluded.verified_at,
                 retrieved_at=excluded.retrieved_at""",
            (signals_json, stamp, stamp),
        )
        con.execute(
            """INSERT INTO person_evidence
               (person_id,kind,url,source,snippet,strength,retrieved_at)
               VALUES (587,'profile','https://www.instagram.com/bartek.kopiniak/',
                       'instagram signed-in profile',?, 'strongest',?)
               ON CONFLICT(person_id,kind,url) DO UPDATE SET
                 source=excluded.source,snippet=excluded.snippet,
                 strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
            ("Public profile is Bartek Kopiniak and independently identifies him as owner and artist manager at @fala.label; the profile links to falalabel.pl.", stamp),
        )
        con.commit()
    finally:
        con.close()
    print(json.dumps({"company_id": company_id, "person_id": 587}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
