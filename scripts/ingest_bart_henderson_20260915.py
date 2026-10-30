"""Add fresh official Fat Wreck evidence and verified Bart Henderson prospect."""
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.normalize import normalize_person_name  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402


def main() -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    now = utcnow()
    company_id = 2067
    try:
        company = con.execute("SELECT roster_estimate,status FROM companies WHERE company_id=?", (company_id,)).fetchone()
        if not company or company["status"] != "QUALIFIED" or not 3 <= int(company["roster_estimate"]) <= 30:
            raise RuntimeError("Fat Wreck company/roster gate failed")
        activities = [
            ("Fat Wreck 35th anniversary multi-artist shows and seven label represses", "https://fatwreck.com/blogs/news/celebrating-35-years-fat-wreck-chords-35th-anniversary-shows-bottom-of-the-hill", "2026-07-17", "fatwreck:35th:2026"),
            ("Western Addiction album 'Psychedelic Munitions' announced for release", "https://fatwreck.com/blogs/news", "2026-05-22", "fatwreck:western-addiction:2026-05-22"),
        ]
        for description, url, observed_at, external_id in activities:
            con.execute(
                """INSERT OR IGNORE INTO activity_signals
                   (company_id,signal_type,description,evidence_url,observed_at,recorded_at,source,external_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (company_id, "release_activity", description, url, observed_at, now, "official_website", external_id),
            )
            con.execute(
                """INSERT INTO company_evidence(company_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(company_id,kind,url) DO UPDATE SET snippet=excluded.snippet,strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                (company_id, "activity", url, "official_website", description, "strongest", now),
            )
        name = "Bart Henderson"
        title = "Label Operations Manager / Controller"
        url = "https://www.linkedin.com/in/bart-henderson"
        role = classify_title(title, small_company=True)
        if not role.qualifies:
            raise RuntimeError("Bart Henderson role gate failed")
        if con.execute("SELECT 1 FROM profiles WHERE channel='linkedin' AND lower(rtrim(url,'/'))=lower(rtrim(?,'/'))", (url,)).fetchone():
            print("duplicate")
            con.commit()
            return 0
        cur = con.execute(
            """INSERT INTO people
               (company_id,full_name,name_normalized,title,title_family,seniority,role_relevance,
                current_verified,employment_sources,status,first_seen,last_researched)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (company_id, name, normalize_person_name(name), title, role.family, role.seniority,
             role.relevance, 1, 2, "VERIFIED", now, now),
        )
        person_id = int(cur.lastrowid)
        con.execute(
            """INSERT INTO profiles(person_id,channel,url,handle,verification_status,usability,verified_at,retrieved_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (person_id, "linkedin", url, "bart-henderson", "VERIFIED", "full", now, now),
        )
        con.execute(
            """INSERT INTO person_evidence(person_id,kind,url,source,snippet,strength,retrieved_at)
               VALUES (?,?,?,?,?,?,?)""",
            (person_id, "employment", url, "public_linkedin_20260915",
             "Public LinkedIn currently lists Fat Wreck Chords; current staff directories identify Bart Henderson as Label Operations Manager / Controller.",
             "strongest", now),
        )
        con.commit()
        print(f"inserted person_id={person_id}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
