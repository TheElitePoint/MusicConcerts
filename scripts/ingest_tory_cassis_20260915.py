"""Insert SOP-verified Paper Bag Records president Tory Cassis."""
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
    try:
        company_id, name, title = 2084, "Tory Cassis", "President"
        url = "https://www.linkedin.com/in/torycassis"
        company = con.execute("SELECT roster_estimate,status FROM companies WHERE company_id=?", (company_id,)).fetchone()
        if not company or company["status"] != "QUALIFIED" or not 3 <= int(company["roster_estimate"]) <= 30:
            raise RuntimeError("Paper Bag company/roster gate failed")
        if con.execute("SELECT 1 FROM profiles WHERE channel='linkedin' AND lower(rtrim(url,'/'))=lower(rtrim(?,'/'))", (url,)).fetchone():
            print("duplicate")
            return 0
        role = classify_title(title, small_company=True)
        if not role.qualifies:
            raise RuntimeError("role gate failed")
        now = utcnow()
        cur = con.execute(
            """INSERT INTO people(company_id,full_name,name_normalized,title,title_family,seniority,
               role_relevance,current_verified,employment_sources,status,first_seen,last_researched)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (company_id, name, normalize_person_name(name), title, role.family, role.seniority,
             role.relevance, 1, 3, "VERIFIED", now, now),
        )
        person_id = int(cur.lastrowid)
        con.execute(
            """INSERT INTO profiles(person_id,channel,url,handle,verification_status,usability,verified_at,retrieved_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (person_id, "linkedin", url, "torycassis", "VERIFIED", "full", now, now),
        )
        note = "Current public LinkedIn profile and current Paper Bag Records company employee listing show Tory Cassis at Paper Bag Records; the company page identifies him as President, corroborated by Canadian music-industry coverage."
        con.execute(
            "INSERT INTO person_evidence(person_id,kind,url,source,snippet,strength,retrieved_at) VALUES (?,?,?,?,?,?,?)",
            (person_id, "employment", url, "public_linkedin_20260915", note, "strongest", now),
        )
        con.commit()
        print(f"inserted person_id={person_id}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
