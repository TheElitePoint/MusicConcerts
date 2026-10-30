"""Insert public-source-verified LinkedIn prospects at newly mapped labels."""
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.normalize import normalize_person_name  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402


ROWS = [
    {
        "company_id": 2072, "name": "Ryan Weiler", "title": "Label Manager",
        "url": "https://www.linkedin.com/in/ryanweiler",
        "note": "Public LinkedIn profile currently lists Big Crown Records; the current LinkedIn company page lists Ryan as an employee, and a current music-company directory identifies him as Label Manager.",
    },
    {
        "company_id": 2148, "name": "Mark Meeuwissen", "title": "A&R Manager",
        "url": "https://www.linkedin.com/in/mark-m-3975581b",
        "note": "Public LinkedIn profile currently lists Black Hole Recordings B.V.; the current company page lists Mark as an employee, and official Black Hole content identifies Mark Meeuwissen as A&R.",
    },
]


def main() -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    try:
        now = utcnow()
        for item in ROWS:
            company = con.execute(
                "SELECT canonical_name,roster_estimate,status FROM companies WHERE company_id=?",
                (item["company_id"],),
            ).fetchone()
            if not company or company["status"] != "QUALIFIED":
                raise RuntimeError(f"Company {item['company_id']} is not qualified")
            if con.execute(
                "SELECT 1 FROM profiles WHERE channel='linkedin' AND lower(rtrim(url,'/'))=lower(rtrim(?,'/'))",
                (item["url"],),
            ).fetchone():
                print(f"skipped duplicate url={item['url']}")
                continue
            norm = normalize_person_name(item["name"])
            if con.execute(
                "SELECT 1 FROM people WHERE company_id=? AND name_normalized=?",
                (item["company_id"], norm),
            ).fetchone():
                print(f"skipped duplicate person={item['name']}")
                continue
            role = classify_title(item["title"], small_company=(company["roster_estimate"] or 99) <= 20)
            if not role.qualifies:
                raise RuntimeError(f"Role gate failed for {item['name']}")
            cur = con.execute(
                """INSERT INTO people
                   (company_id,full_name,name_normalized,title,title_family,seniority,
                    role_relevance,current_verified,employment_sources,status,first_seen,last_researched)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item["company_id"], item["name"], norm, item["title"], role.family,
                 role.seniority, role.relevance, 1, 2, "VERIFIED", now, now),
            )
            person_id = int(cur.lastrowid)
            con.execute(
                """INSERT INTO profiles
                   (person_id,channel,url,handle,verification_status,usability,verified_at,retrieved_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (person_id, "linkedin", item["url"], item["url"].rsplit("/", 1)[-1],
                 "VERIFIED", "full", now, now),
            )
            con.execute(
                """INSERT INTO person_evidence
                   (person_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (person_id, "employment", item["url"], "public_linkedin_20260915",
                 item["note"], "strongest", now),
            )
            print(f"inserted person_id={person_id} name={item['name']} company={company['canonical_name']}")
        con.commit()
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
