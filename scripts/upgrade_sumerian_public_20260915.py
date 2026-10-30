"""Record fresh official Sumerian activity and admit two public-verified staff."""
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.normalize import normalize_person_name  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402

COMPANY_ID = 1114
ACTIVITY = [
    ("Roman Candle album 'Unadulterated' official pre-order release", "https://sumerianrecords.com/collections/music/products/roman-candle-unadulterated-vinyl-red-purple-orange-side-a-b", "2026-09-25", "sumerian:roman-candle:unadulterated:2026-09-25"),
    ("Holding Absence album 'Modern Life Is Lonely' official pre-order release", "https://sumerianrecords.com/products/holding-absence-modern-life-is-lonely-cd-digipak", "2026-09-18", "sumerian:holding-absence:modern-life-is-lonely:2026-09-18"),
]
PEOPLE = [
    (1683, "Mike Jakubow", "Vice President Label & Head Of Radio Promotions", "https://www.linkedin.com/in/mikejakubow", "Public LinkedIn profile currently lists Sumerian Records and states Vice President Label & Head Of Radio Promotions."),
]


def main() -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    now = utcnow()
    try:
        company = con.execute("SELECT roster_estimate,status FROM companies WHERE company_id=?", (COMPANY_ID,)).fetchone()
        if not company or company["status"] != "QUALIFIED" or not 3 <= int(company["roster_estimate"]) <= 30:
            raise RuntimeError("Sumerian company gate/roster gate failed")
        for description, url, observed_at, external_id in ACTIVITY:
            con.execute(
                """INSERT OR IGNORE INTO activity_signals
                   (company_id,signal_type,description,evidence_url,observed_at,recorded_at,source,external_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (COMPANY_ID, "release_activity", description, url, observed_at, now, "official_website", external_id),
            )
            con.execute(
                """INSERT INTO company_evidence(company_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(company_id,kind,url) DO UPDATE SET snippet=excluded.snippet,strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                (COMPANY_ID, "activity", url, "official_website", description, "strongest", now),
            )
        for existing_id, name, title, url, note in PEOPLE:
            role = classify_title(title, small_company=False)
            if not role.qualifies:
                raise RuntimeError(f"Role gate failed for {name}")
            person_id = existing_id
            if person_id is None:
                duplicate = con.execute(
                    "SELECT person_id FROM profiles WHERE channel='linkedin' AND lower(rtrim(url,'/'))=lower(rtrim(?,'/'))",
                    (url,),
                ).fetchone()
                if duplicate:
                    person_id = int(duplicate[0])
                else:
                    norm = normalize_person_name(name)
                    current = con.execute(
                        "SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
                        (COMPANY_ID, norm),
                    ).fetchone()
                    if current:
                        person_id = int(current[0])
                    else:
                        cur = con.execute(
                            """INSERT INTO people
                               (company_id,full_name,name_normalized,title,title_family,seniority,role_relevance,
                                current_verified,employment_sources,status,first_seen,last_researched)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (COMPANY_ID, name, norm, title, role.family, role.seniority, role.relevance,
                             1, 2, "VERIFIED", now, now),
                        )
                        person_id = int(cur.lastrowid)
                        con.execute(
                            """INSERT INTO profiles
                               (person_id,channel,url,handle,verification_status,usability,verified_at,retrieved_at)
                               VALUES (?,?,?,?,?,?,?,?)""",
                            (person_id, "linkedin", url, url.rsplit("/", 1)[-1], "VERIFIED", "full", now, now),
                        )
            con.execute(
                """UPDATE people SET title=?,title_family=?,seniority=?,role_relevance=?,current_verified=1,
                          employment_sources=2,status='VERIFIED',reject_reason=NULL,last_researched=? WHERE person_id=?""",
                (title, role.family, role.seniority, role.relevance, now, person_id),
            )
            con.execute(
                """UPDATE profiles SET url=?,handle=?,verification_status='VERIFIED',usability='full',verified_at=?,retrieved_at=?
                    WHERE person_id=? AND channel='linkedin'""",
                (url, url.rsplit("/", 1)[-1], now, now, person_id),
            )
            con.execute(
                """INSERT INTO person_evidence(person_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(person_id,kind,url) DO UPDATE SET snippet=excluded.snippet,strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                (person_id, "employment", url, "public_linkedin_20260915", note, "strongest", now),
            )
            print(f"verified person_id={person_id} name={name}")
        con.commit()
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
