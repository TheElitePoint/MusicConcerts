"""Canonicalize and admit Hospital Records people verified on public LinkedIn."""
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402


ROWS = {
    2952: {
        "name": "Chris Goss", "title": "Co-Founder/CEO",
        "url": "https://www.linkedin.com/in/chris-goss-9b1331203",
        "note": "Public LinkedIn profile shows Hospital Records Limited as current employer and current 2026 company activity; an independent current board-member profile identifies Chris Goss as Co-Founder and CEO.",
    },
    2954: {
        "name": "Matt Lally", "title": "Label Manager",
        "url": "https://www.linkedin.com/in/mattlally97",
        "note": "Public LinkedIn profile shows Hospital Records Limited as current employer; current 2026 posts and conference activity confirm Matt Lally remains on the label team. Discovery data identifies the current title as Label Manager.",
    },
    2956: {
        "name": "Craig Dawson", "title": "Managing Director",
        "url": "https://www.linkedin.com/in/craig-dawson-1b156225",
        "note": "Public LinkedIn profile shows Hospital Records Limited as current employer and multiple current 2026 label posts; discovery data identifies the current title as Managing Director.",
    },
}


def main() -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    try:
        now = utcnow()
        for person_id, item in ROWS.items():
            row = con.execute(
                """SELECT p.full_name,c.canonical_name,c.roster_estimate
                     FROM people p JOIN companies c USING(company_id)
                    WHERE p.person_id=? AND c.status='QUALIFIED'""",
                (person_id,),
            ).fetchone()
            if not row or row["full_name"] != item["name"] or row["canonical_name"] != "Hospital Records":
                raise RuntimeError(f"Unexpected identity for person_id={person_id}")
            role = classify_title(item["title"], small_company=(row["roster_estimate"] or 99) <= 20)
            if not role.qualifies:
                raise RuntimeError(f"SOP role gate failed for person_id={person_id}")
            duplicate = con.execute(
                """SELECT person_id FROM profiles
                    WHERE channel='linkedin' AND lower(rtrim(url,'/'))=lower(rtrim(?,'/'))
                      AND person_id<>?""",
                (item["url"], person_id),
            ).fetchone()
            if duplicate:
                raise RuntimeError(f"Duplicate LinkedIn URL already belongs to {duplicate[0]}")
            con.execute(
                """UPDATE people SET title=?,title_family=?,seniority=?,role_relevance=?,
                          current_verified=1,employment_sources=2,status='VERIFIED',
                          reject_reason=NULL,last_researched=? WHERE person_id=?""",
                (item["title"], role.family, role.seniority, role.relevance, now, person_id),
            )
            con.execute(
                """UPDATE profiles SET url=?,handle=?,verification_status='VERIFIED',
                          usability='full',verified_at=?,retrieved_at=?
                    WHERE person_id=? AND channel='linkedin'""",
                (item["url"], item["url"].rsplit("/", 1)[-1], now, now, person_id),
            )
            con.execute(
                """INSERT INTO person_evidence
                   (person_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(person_id,kind,url) DO UPDATE SET
                     source=excluded.source,snippet=excluded.snippet,
                     strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                (person_id, "employment", item["url"], "public_linkedin_20260915",
                 item["note"], "strongest", now),
            )
            print(f"verified person_id={person_id} name={item['name']}")
        con.commit()
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
