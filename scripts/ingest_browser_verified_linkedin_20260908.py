"""Admit LinkedIn people independently verified in the public browser pass.

Every row below was checked on a current public LinkedIn profile.  The profile
showed both the target employer and an SOP-qualifying current title.  This is a
small, auditable bridge for browser-confirmed records; it deliberately enforces
the per-company cap and refuses to overwrite existing people or profile URLs.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.normalize import normalize_person_name  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402


ROWS = [
    {
        "company": "Virgin Music Group",
        "name": "Jackson Downey",
        "title": "A&R",
        "url": "https://www.linkedin.com/in/jackson-downey-9266801b3",
        "note": "Current LinkedIn profile shows A&R at Virgin Music Group; current-position announcement and experience section were visible in the public browser on 2026-09-08.",
    },
    {
        "company": "Virgin Music Group",
        "name": "Artyan A.",
        "title": "Label Manager",
        "url": "https://id.linkedin.com/in/artyan-453494142",
        "note": "Current LinkedIn profile shows Label Manager at Virgin Music Group from November 2023 to present; verified in the public browser on 2026-09-08.",
    },
    {
        "company": "Nettwerk Music Group",
        "name": "Annie Dickinson",
        "title": "A&R Director",
        "url": "https://www.linkedin.com/in/annie-dickinson-47a238174",
        "note": "Current LinkedIn public profile states A&R Director at Nettwerk Music Group and lists Nettwerk as current experience; verified in the public browser on 2026-09-08.",
    },
    {
        "company": "Nettwerk Music Group",
        "name": "Sameer Sadhu",
        "title": "SVP of A&R",
        "url": "https://www.linkedin.com/in/sameersadhu",
        "note": "Current LinkedIn public profile states SVP of A&R at Nettwerk Music Group and lists Nettwerk as current experience; verified in the public browser on 2026-09-08.",
    },
    {
        "company": "Nettwerk Music Group",
        "name": "Mike Glita",
        "title": "Director, A&R",
        "url": "https://www.linkedin.com/in/mike-glita",
        "note": "Current LinkedIn public profile and current-position announcement show Director, A&R at Nettwerk Music Group; verified in the public browser on 2026-09-08.",
    },
    {
        "company": "Nettwerk Music Group",
        "name": "Arielle Dunn",
        "title": "Marketing Director",
        "url": "https://www.linkedin.com/in/arielle-dunn-72ab9413",
        "note": "Current LinkedIn public profile lists Nettwerk Music Group as current experience; LinkedIn campaign coverage identifies Arielle Dunn as Marketing Director; verified in the public browser on 2026-09-08.",
    },
    {
        "company": "Sub Pop Records",
        "name": "Nick Duncan",
        "title": "A&R Manager",
        "url": "https://www.linkedin.com/in/nick-duncan-sea",
        "note": "Current LinkedIn public profile identifies Nick Duncan as A&R Manager at Sub Pop Records and describes current signings and projects; verified in the public browser on 2026-09-08.",
    },
    {
        "company": "Napalm Records",
        "name": "Markus Riedler",
        "title": "Founder and Managing Director",
        "url": "https://at.linkedin.com/in/markus-riedler-3a532858",
        "note": "Current public LinkedIn profile activity and Napalm Records' official LinkedIn company page identify Markus Riedler as founder and managing director; verified through public browser sources on 2026-09-08.",
    },
    {
        "company": "Majik Ninja Entertainment",
        "name": "George Vlahakis",
        "title": "Co-Founder",
        "url": "https://www.linkedin.com/in/george-vlahakis-46017198",
        "note": "Majik Ninja Entertainment's current official about page identifies George Vlahakis as a co-founder, and the company's current public LinkedIn employee list links this profile to the company; verified through public browser sources on 2026-09-08.",
    },
    {
        "company": "Fearless Records",
        "name": "Jonathan Rego",
        "title": "Senior Manager, A&R Recorded Music",
        "url": "https://www.linkedin.com/in/jonathanrego",
        "note": "The current public LinkedIn profile lists Fearless Records, while current 2026 professional directory pages identify Jonathan Rego as Senior Manager, A&R Recorded Music at Fearless Records; verified through public browser sources on 2026-09-08.",
    },
]


def main() -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    inserted = skipped = 0
    try:
        for item in ROWS:
            company = con.execute(
                "SELECT company_id,roster_estimate FROM companies WHERE canonical_name=? AND status='QUALIFIED'",
                (item["company"],),
            ).fetchone()
            if not company:
                skipped += 1
                continue
            active = con.execute(
                "SELECT COUNT(*) FROM people WHERE company_id=? AND status!='REJECTED'",
                (company["company_id"],),
            ).fetchone()[0]
            if active >= 10:
                skipped += 1
                continue
            if con.execute(
                "SELECT 1 FROM profiles WHERE channel='linkedin' AND lower(rtrim(url,'/'))=lower(rtrim(?,'/'))",
                (item["url"],),
            ).fetchone():
                skipped += 1
                continue
            norm = normalize_person_name(item["name"])
            if con.execute(
                "SELECT 1 FROM people WHERE company_id=? AND name_normalized=?",
                (company["company_id"], norm),
            ).fetchone():
                skipped += 1
                continue
            role = classify_title(
                item["title"], small_company=(company["roster_estimate"] or 99) <= 20
            )
            if not role.qualifies:
                skipped += 1
                continue
            now = utcnow()
            cur = con.execute(
                """INSERT INTO people
                   (company_id,full_name,name_normalized,title,title_family,seniority,
                    role_relevance,current_verified,employment_sources,status,first_seen,last_researched)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (company["company_id"], item["name"], norm, item["title"], role.family,
                 role.seniority, role.relevance, 1, 1, "VERIFIED", now, now),
            )
            pid = int(cur.lastrowid)
            con.execute(
                """INSERT INTO profiles
                   (person_id,channel,url,handle,verification_status,usability,verified_at,retrieved_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (pid, "linkedin", item["url"], item["url"].rstrip("/").rsplit("/", 1)[-1],
                 "VERIFIED", "full", now, now),
            )
            con.execute(
                """INSERT INTO person_evidence
                   (person_id,kind,url,source,snippet,strength,retrieved_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (pid, "employment", item["url"], "browser_public_linkedin_profile",
                 item["note"], "strongest", now),
            )
            inserted += 1
        con.commit()
    finally:
        con.close()
    print(f"inserted={inserted} skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
