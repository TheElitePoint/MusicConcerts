"""Build the separate Instagram Final, search log and review datasets."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.stages.s8_score import build_facts  # noqa: E402
from mp.stages.s9_export import _mandatory_ok, _row_for  # noqa: E402

OUTPUT = ROOT / "data" / "instagram_separate_run_20260830.json"
HANDLE_AUDIT = ROOT / "data" / "instagram_runs" / "handle_candidates_20260829T192839Z" / "handle_discovery.json"
TAG_AUDIT = ROOT / "data" / "instagram_runs" / "company_tags_20260829T192109Z" / "company_tag_discovery.json"
PROFILE_AUDIT = ROOT / "data" / "instagram_runs" / "20260829T191846Z" / "profile_verification.json"

SEARCH_COLUMNS = [
    "Company Name", "Person Name", "Current Job Title", "Person LinkedIn URL",
    "Search Method", "Candidate Handles Checked", "Candidate Instagram URL",
    "Search Result", "Identity Evidence URL", "Evidence Notes",
]
REVIEW_COLUMNS = [
    "Company Name", "Person Name", "Current Job Title", "Candidate Instagram URL",
    "Person LinkedIn URL", "Instagram Verification", "Identity Score", "Usability",
    "Qualification Score", "Verdict", "Missing / Reason",
]


def read_json(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def main() -> int:
    sop = config.sop()
    final_columns = sop["sheets"]["instagram"]["columns"]
    mandatory = sop["sheets"]["instagram"]["mandatory"]
    not_found = sop["sheets"]["not_found_literal"]
    ctx = Context.create(db_path="data/master.sqlite")
    final_rows: list[dict] = []
    review_rows: list[dict] = []
    seen_final: set[str] = set()
    per_company: dict[int, int] = defaultdict(int)
    try:
        scores = ctx.db.q(
            """SELECT s.*,p.company_id,pr.url AS instagram_url
                 FROM scores s JOIN people p USING(person_id)
                 JOIN companies c USING(company_id)
                 JOIN profiles pr ON pr.person_id=p.person_id AND pr.channel='instagram'
                WHERE s.channel='instagram' AND s.verdict='FINAL PASS'
                  AND p.status!='REJECTED' AND c.status='QUALIFIED'
                  AND pr.verification_status='VERIFIED'
                ORDER BY s.total DESC,c.canonical_name,p.full_name"""
        )
        for score_row in scores:
            facts = build_facts(ctx, score_row["person_id"])
            key = (facts.get("instagram_url") or "").lower().rstrip("/")
            if not key or key in seen_final or per_company[facts["company_id"]] >= 10:
                continue
            row = _row_for(facts, score_row, "instagram", sop)
            ok, _ = _mandatory_ok(row, mandatory, not_found)
            if not ok:
                continue
            final_rows.append({column: row.get(column, "") for column in final_columns})
            seen_final.add(key)
            per_company[facts["company_id"]] += 1

        for row in ctx.db.q(
            """SELECT c.canonical_name,p.person_id,p.full_name,p.title,pr.url,
                      pr.verification_status,pr.identity_score,pr.usability,
                      s.total,s.verdict,s.failed_gates,
                      li.url AS linkedin_url
                 FROM profiles pr JOIN people p USING(person_id)
                 JOIN companies c USING(company_id)
                 LEFT JOIN scores s ON s.person_id=p.person_id AND s.channel='instagram'
                 LEFT JOIN profiles li ON li.person_id=p.person_id AND li.channel='linkedin'
                WHERE pr.channel='instagram' AND p.status!='REJECTED'
                  AND c.status='QUALIFIED'
                  AND NOT (pr.verification_status='VERIFIED' AND s.verdict='FINAL PASS')
                ORDER BY COALESCE(s.total,0) DESC,c.canonical_name,p.full_name"""
        ):
            gates = json.loads(row["failed_gates"] or "[]")
            if row["verification_status"] != "VERIFIED":
                reason = "Personal Instagram identity/company relationship not sufficiently confirmed."
            elif row["verdict"] == "RESEARCH FURTHER":
                reason = "Verified Instagram, but qualification score remains below 90."
            else:
                reason = ", ".join(gates) or "Does not meet all Instagram Final gates."
            review_rows.append({
                "Company Name": row["canonical_name"], "Person Name": row["full_name"],
                "Current Job Title": row["title"] or "", "Candidate Instagram URL": row["url"],
                "Person LinkedIn URL": row["linkedin_url"] or not_found,
                "Instagram Verification": row["verification_status"],
                "Identity Score": row["identity_score"] if row["identity_score"] is not None else "",
                "Usability": row["usability"] or "", "Qualification Score": row["total"] or "",
                "Verdict": row["verdict"] or "NOT SCORED", "Missing / Reason": reason,
            })
    finally:
        ctx.close()

    tag_by_person = {int(r["person_id"]): r for r in read_json(TAG_AUDIT)
                     if r.get("result") == "VERIFIED"}
    search_rows: list[dict] = []
    searched_people: set[int] = set()
    for item in read_json(HANDLE_AUDIT):
        person_id = int(item["person_id"])
        searched_people.add(person_id)
        tag = tag_by_person.get(person_id)
        attempts = item.get("handles_checked") or []
        handles = "; ".join(
            f"@{a.get('handle')} ({'found' if a.get('found') else 'not found'})" for a in attempts
        ) or "Not applicable"
        if tag:
            method = "Official company Instagram post tag"
            result = "VERIFIED"
            profile_url = tag.get("person_instagram_url") or ""
            evidence_url = tag.get("company_tag_evidence_url") or ""
            notes = "Official company account tagged the person; public profile name matched the verified LinkedIn identity."
        elif item.get("result") == "ALREADY_VERIFIED":
            method = "Existing candidate rechecked with public profile metadata"
            result = "VERIFIED"
            profile_url = item.get("person_instagram_url") or ""
            evidence_url = profile_url
            notes = "Personal profile already passed the SOP identity and usability checks."
        else:
            method = "Verified-name handle candidates + public profile metadata"
            result = item.get("result") or "NOT VERIFIED"
            profile_url = item.get("person_instagram_url") or not_found
            evidence_url = profile_url if profile_url != not_found else not_found
            notes = "No checked candidate confirmed both the person identity and current company relationship."
        search_rows.append({
            "Company Name": item.get("company") or "", "Person Name": item.get("person") or "",
            "Current Job Title": item.get("title") or "",
            "Person LinkedIn URL": item.get("linkedin_url") or not_found,
            "Search Method": method, "Candidate Handles Checked": handles,
            "Candidate Instagram URL": profile_url, "Search Result": result,
            "Identity Evidence URL": evidence_url, "Evidence Notes": notes,
        })

    for item in read_json(PROFILE_AUDIT):
        person_id = int(item["person_id"])
        if person_id in searched_people:
            continue
        search_rows.append({
            "Company Name": item.get("company") or "", "Person Name": item.get("person") or "",
            "Current Job Title": item.get("title") or "", "Person LinkedIn URL": not_found,
            "Search Method": "Stored candidate + public profile metadata",
            "Candidate Handles Checked": "@" + (item.get("handle") or ""),
            "Candidate Instagram URL": item.get("url") or not_found,
            "Search Result": item.get("result") or "UNAVAILABLE",
            "Identity Evidence URL": item.get("url") or not_found,
            "Evidence Notes": "Public profile name, bio, links and company signals were scored under SOP SS35.",
        })

    OUTPUT.write_text(json.dumps({
        "final_columns": final_columns, "final_rows": final_rows,
        "search_columns": SEARCH_COLUMNS, "search_rows": search_rows,
        "review_columns": REVIEW_COLUMNS, "review_rows": review_rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "final_rows": len(final_rows), "final_companies": len(per_company),
        "max_per_company": max(per_company.values(), default=0),
        "search_rows": len(search_rows), "review_rows": len(review_rows),
        "output": str(OUTPUT),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
