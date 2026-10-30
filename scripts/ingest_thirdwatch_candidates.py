"""Admit structured Thirdwatch discovery rows as unverified LinkedIn candidates.

Only rows with an explicit matchedCurrentCompanies value and a qualifying role
are stored. They remain unverified until a separate fresh profile actor confirms
the current employer and title.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from expand_salesnav_batched import _historical_urls, _human_name  # noqa: E402
from expand_salesnav_filters import _employer_key  # noqa: E402
from mp.db import utcnow  # noqa: E402
from mp.normalize import linkedin_person_url, normalize_person_name  # noqa: E402
from mp.sop.roles import classify_title  # noqa: E402


def _title(item: dict, company: str) -> str:
    headline = str(item.get("headline") or "").strip()
    marker = " at "
    if marker in headline.lower():
        before, after = headline.rsplit(marker, 1)
        if _employer_key(after) == _employer_key(company):
            return before.strip()
    matches = item.get("matchedJobTitles") or []
    return str(matches[0]).strip() if matches else headline


def main(paths: list[str]) -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    try:
        companies = {
            _employer_key(row["canonical_name"]): row
            for row in con.execute(
                "SELECT company_id,canonical_name,roster_estimate FROM companies WHERE status='QUALIFIED'"
            )
        }
        seen = {str(row[0]).lower() for row in con.execute(
            "SELECT url FROM profiles WHERE channel='linkedin'"
        )} | _historical_urls()
        per_company: defaultdict[int, int] = defaultdict(int)
        stats: defaultdict[str, int] = defaultdict(int)
        for raw_path in paths:
            payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
            items = payload.get("items") if isinstance(payload, dict) else payload
            for item in items or []:
                stats["rows"] += 1
                matched = item.get("matchedCurrentCompanies") or []
                targets = [companies.get(_employer_key(str(name))) for name in matched]
                targets = [target for target in targets if target is not None]
                if len(targets) != 1:
                    stats["company_rejects"] += 1
                    continue
                target = targets[0]
                name = str(item.get("name") or item.get("fullName") or "").strip()
                url = linkedin_person_url(str(item.get("profileUrl") or item.get("url") or ""))
                if not (_human_name(name) and url):
                    stats["identity_rejects"] += 1
                    continue
                if url.lower() in seen:
                    stats["duplicates"] += 1
                    continue
                title = _title(item, target["canonical_name"])
                role = classify_title(
                    title,
                    small_company=(target["roster_estimate"] or 99) <= 20,
                )
                if not role.qualifies:
                    stats["role_rejects"] += 1
                    continue
                if per_company[target["company_id"]] >= 25:
                    stats["candidate_cap_rejects"] += 1
                    continue
                norm = normalize_person_name(name)
                con.execute(
                    """INSERT INTO people
                       (company_id,full_name,name_normalized,title,title_family,seniority,
                        role_relevance,status,first_seen)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(company_id,name_normalized) DO UPDATE SET
                         title=CASE WHEN people.current_verified=0 THEN excluded.title ELSE people.title END,
                         title_family=CASE WHEN people.current_verified=0 THEN excluded.title_family ELSE people.title_family END,
                         seniority=CASE WHEN people.current_verified=0 THEN excluded.seniority ELSE people.seniority END,
                         role_relevance=CASE WHEN people.current_verified=0 THEN excluded.role_relevance ELSE people.role_relevance END""",
                    (target["company_id"], name, norm, title, role.family,
                     role.seniority, role.relevance, "CANDIDATE", utcnow()),
                )
                person = con.execute(
                    "SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
                    (target["company_id"], norm),
                ).fetchone()
                if not person:
                    continue
                pid = int(person[0])
                cur = con.execute(
                    """INSERT OR IGNORE INTO profiles
                       (person_id,channel,url,handle,verification_status,retrieved_at)
                       VALUES (?,?,?,?,?,?)""",
                    (pid, "linkedin", url, url.rstrip("/").rsplit("/", 1)[-1],
                     "UNVERIFIED", utcnow()),
                )
                if not cur.rowcount:
                    stats["duplicates"] += 1
                    seen.add(url.lower())
                    continue
                snippet = str(item.get("sourceSnippet") or "").strip()
                con.execute(
                    """INSERT INTO person_evidence
                       (person_id,kind,url,source,snippet,strength,retrieved_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(person_id,kind,url) DO UPDATE SET
                         source=excluded.source,snippet=excluded.snippet,
                         strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
                    (pid, "employment", url, "thirdwatch_candidate_finder",
                     f"Structured current-company match: {title} at {target['canonical_name']}; {snippet}"[:1500],
                     "weak", utcnow()),
                )
                seen.add(url.lower())
                per_company[target["company_id"]] += 1
                stats["inserted"] += 1
        con.commit()
        print(" ".join(f"{key}={value}" for key, value in sorted(stats.items())))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    raise SystemExit(main(args.paths))
