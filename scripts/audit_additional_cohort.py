"""Audit the additional LinkedIn cohort against every delivered URL and exact employer IDs."""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from mp.normalize import linkedin_person_url, linkedin_slug  # noqa: E402
from verify_profiles_apimaestro import _current_experience, _employer_matches  # noqa: E402


def delivered_urls() -> set[str]:
    urls: set[str] = set()
    for path in ROOT.glob("runs/*/linkedin_final.csv"):
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                value = linkedin_person_url(row.get("Person LinkedIn URL", ""))
                if value:
                    urls.add(value.lower())
    prior = ROOT / "data/net_new_linkedin_final.json"
    if prior.exists():
        for row in json.loads(prior.read_text(encoding="utf-8")).get("rows", []):
            value = linkedin_person_url(row.get("Person LinkedIn URL", ""))
            if value:
                urls.add(value.lower())
    return urls


def company_directory() -> dict[int, dict[str, Any]]:
    path = ROOT / "data/linkedin_company_directory.json"
    if not path.exists():
        return {}
    return {int(row["company_id"]): row for row in json.loads(path.read_text(encoding="utf-8"))}


def profile_items() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(ROOT.glob("data/acquisition/data_slayer_profile_verification_*/*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            value = str(item.get("profile_link") or item.get("linkedin_url") or "")
            _kind, slug = linkedin_slug(value)
            if slug:
                out[slug.lower()] = {**item, "_raw_path": str(path.relative_to(ROOT))}
    return out


def page_slug(url: str) -> str:
    path = urlparse(url or "").path.strip("/").split("/")
    if len(path) >= 2 and path[0].lower() == "company":
        return path[1].lower()
    return ""


def current_org(item: dict[str, Any], expected: str) -> tuple[str, str, str, str]:
    expected_company, title = _current_experience(item, expected)
    experiences = item.get("experience") or []
    if isinstance(experiences, dict):
        experiences = list(experiences.values())
    for exp in experiences if isinstance(experiences, list) else []:
        if not isinstance(exp, dict) or not exp.get("job_still_working"):
            continue
        name = str(exp.get("company_name") or exp.get("raw_company_name") or "").strip()
        if _employer_matches(expected, name):
            return (
                name,
                title,
                str(exp.get("company_id") or "").strip(),
                str(exp.get("company_url") or "").strip(),
            )
    return (
        expected_company,
        title,
        str(item.get("company_id") or "").strip(),
        str(item.get("current_company_linkedin_url") or "").strip(),
    )


def main() -> int:
    delivered = delivered_urls()
    directory = company_directory()
    raw = profile_items()
    con = sqlite3.connect(ROOT / "data/master.sqlite")
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT s.person_id,s.total,p.company_id,p.full_name,p.title,
                      c.canonical_name,c.linkedin_url,pr.url
                 FROM scores s
                 JOIN people p USING(person_id)
                 JOIN companies c USING(company_id)
                 JOIN profiles pr ON pr.person_id=p.person_id AND pr.channel='linkedin'
                WHERE s.channel='linkedin' AND s.verdict='FINAL PASS'
                  AND p.status!='REJECTED' AND c.status='QUALIFIED'
                  AND pr.verification_status='VERIFIED'
                ORDER BY s.total DESC,c.canonical_name,p.full_name"""
        ).fetchall()
        near_rows = con.execute(
            """SELECT s.person_id,s.total,s.subscores,p.company_id,p.full_name,p.title,
                      p.current_verified,c.canonical_name,c.linkedin_url,pr.url
                 FROM scores s
                 JOIN people p USING(person_id)
                 JOIN companies c USING(company_id)
                 JOIN profiles pr ON pr.person_id=p.person_id AND pr.channel='linkedin'
                WHERE s.channel='linkedin' AND s.total BETWEEN 82 AND 89
                  AND p.status!='REJECTED' AND c.status='QUALIFIED'
                  AND pr.verification_status='VERIFIED'
                ORDER BY s.total DESC,c.canonical_name,p.full_name"""
        ).fetchall()
    finally:
        con.close()

    audited: list[dict[str, Any]] = []
    seen: set[str] = set()
    counts: dict[int, int] = defaultdict(int)
    for row in rows:
        url = linkedin_person_url(row["url"] or "")
        if not url or url.lower() in delivered or url.lower() in seen:
            continue
        if counts[int(row["company_id"])] >= 10:
            continue
        _kind, person_slug = linkedin_slug(url)
        item = raw.get(person_slug.lower()) if person_slug else None
        expected = directory.get(int(row["company_id"]), {})
        expected_id = str(expected.get("linkedin_org_id") or "")
        expected_url = str(expected.get("linkedin_url") or row["linkedin_url"] or "")
        found_name = found_title = found_id = found_url = ""
        id_result = "NO_RAW_PROFILE"
        if item:
            found_name, found_title, found_id, found_url = current_org(item, row["canonical_name"])
            if expected_id and found_id:
                id_result = "MATCH" if expected_id == found_id else "MISMATCH"
            elif page_slug(expected_url) and page_slug(found_url):
                id_result = "MATCH" if page_slug(expected_url) == page_slug(found_url) else "MISMATCH"
            else:
                id_result = "UNRESOLVED"
        audited.append({
            "person_id": int(row["person_id"]),
            "company_id": int(row["company_id"]),
            "score": int(row["total"]),
            "person": row["full_name"],
            "title": row["title"],
            "person_url": url,
            "company": row["canonical_name"],
            "expected_org_id": expected_id,
            "expected_company_url": expected_url,
            "found_company": found_name,
            "found_title": found_title,
            "found_org_id": found_id,
            "found_company_url": found_url,
            "identity_result": id_result,
            "raw_path": item.get("_raw_path", "") if item else "",
        })
        seen.add(url.lower())
        counts[int(row["company_id"])] += 1

    folder = ROOT / "data/audits"
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / "additional_linkedin_identity_audit.json"
    output.write_text(json.dumps({"rows": audited}, ensure_ascii=False, indent=2), encoding="utf-8")
    near: list[dict[str, Any]] = []
    for row in near_rows:
        url = linkedin_person_url(row["url"] or "")
        if not url or url.lower() in delivered:
            continue
        near.append({
            "person_id": int(row["person_id"]),
            "company_id": int(row["company_id"]),
            "score": int(row["total"]),
            "subscores": json.loads(row["subscores"] or "{}"),
            "person": row["full_name"],
            "title": row["title"],
            "person_url": url,
            "company": row["canonical_name"],
        })
    near_output = folder / "additional_linkedin_verified_near_misses.json"
    near_output.write_text(json.dumps({"rows": near}, ensure_ascii=False, indent=2), encoding="utf-8")
    by_result: dict[str, int] = defaultdict(int)
    by_company: dict[str, int] = defaultdict(int)
    for row in audited:
        by_result[row["identity_result"]] += 1
        by_company[row["company"]] += 1
    print(json.dumps({
        "rows": len(audited),
        "identity_results": dict(sorted(by_result.items())),
        "companies": dict(sorted(by_company.items(), key=lambda item: (-item[1], item[0]))),
        "verified_near_misses_82_89": len(near),
        "output": str(output),
        "near_miss_output": str(near_output),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
