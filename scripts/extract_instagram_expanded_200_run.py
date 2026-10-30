"""Build a separate 200+ person Instagram research workbook payload.

The FINAL dataset is still governed entirely by the SOP export gates.  The larger
search/review datasets preserve every researched person, including honest negative
results, so unresolved accounts are never presented as verified Instagram leads.
"""
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

OUTPUT = ROOT / "data" / "instagram_expanded_200_run_20260830.json"
SEEDS = [
    ROOT / "data" / "net_new_linkedin_final.json",
    ROOT / "data" / "additional_100_linkedin_final_20260829.json",
]
FOLLOWING = ROOT / "data" / "instagram_runs" / "browser_following_20260830.json"
PROFILE_CHECKS = ROOT / "data" / "instagram_runs" / "browser_profile_checks_20260830.json"

SEARCH_COLUMNS = [
    "Company Name", "Person Name", "Current Job Title", "Person LinkedIn URL",
    "Company Instagram URL", "Search Method", "Candidate Instagram URL",
    "Search Result", "Instagram Profile Status", "Identity Evidence URL",
    "Evidence Notes",
]
REVIEW_COLUMNS = [
    "Company Name", "Person Name", "Current Job Title", "Person LinkedIn URL",
    "Company Instagram URL", "Candidate Instagram URL", "Instagram Verification",
    "Identity Score", "Usability", "Qualification Score", "Verdict",
    "Review Status", "Missing / Reason",
]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def url_key(value: str | None) -> str:
    return (value or "").strip().lower().rstrip("/")


def main() -> int:
    sop = config.sop()
    final_columns = sop["sheets"]["instagram"]["columns"]
    mandatory = sop["sheets"]["instagram"]["mandatory"]
    not_found = sop["sheets"]["not_found_literal"]

    seed_rows: list[dict] = []
    seen_seed: set[str] = set()
    for path in SEEDS:
        for row in load(path)["rows"]:
            key = url_key(row.get("Person LinkedIn URL"))
            if not key or key in seen_seed:
                continue
            seen_seed.add(key)
            seed_rows.append(row)

    following = {url_key(r.get("linkedin_url")): r for r in load(FOLLOWING)}
    checks = {url_key(r.get("linkedin_url")): r for r in load(PROFILE_CHECKS)}

    ctx = Context.create(db_path="data/master.sqlite")
    final_rows: list[dict] = []
    seen_final_profiles: set[str] = set()
    final_linkedin: set[str] = set()
    per_company: dict[int, int] = defaultdict(int)
    profile_by_linkedin: dict[str, dict] = {}
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
            profile_key = url_key(facts.get("instagram_url"))
            if (not profile_key or profile_key in seen_final_profiles
                    or per_company[facts["company_id"]] >= 10):
                continue
            row = _row_for(facts, score_row, "instagram", sop)
            ok, _ = _mandatory_ok(row, mandatory, not_found)
            if not ok:
                continue
            final_rows.append({column: row.get(column, "") for column in final_columns})
            seen_final_profiles.add(profile_key)
            final_linkedin.add(url_key(row.get("Person LinkedIn URL")))
            per_company[facts["company_id"]] += 1

        for row in ctx.db.q(
            """SELECT li.url AS linkedin_url,ig.url AS instagram_url,
                      ig.verification_status,ig.identity_score,ig.usability,
                      s.total,s.verdict,s.failed_gates
                 FROM profiles li
                 JOIN people p ON p.person_id=li.person_id
                 LEFT JOIN profiles ig ON ig.person_id=p.person_id AND ig.channel='instagram'
                 LEFT JOIN scores s ON s.person_id=p.person_id AND s.channel='instagram'
                WHERE li.channel='linkedin'"""
        ):
            key = url_key(row["linkedin_url"])
            if key:
                profile_by_linkedin[key] = dict(row)
    finally:
        ctx.close()

    search_rows: list[dict] = []
    review_rows: list[dict] = []
    for seed in seed_rows:
        linkedin_url = seed.get("Person LinkedIn URL") or not_found
        key = url_key(linkedin_url)
        follow = following.get(key)
        check = checks.get(key)
        db_profile = profile_by_linkedin.get(key, {})
        company_ig = seed.get("Company Instagram URL") or not_found
        candidate = (
            (check or {}).get("person_instagram_url")
            or (follow or {}).get("person_instagram_url")
            or db_profile.get("instagram_url")
            or not_found
        )

        if check:
            method = "Official company Following list + signed-in direct profile check"
            result = "VERIFIED COMPANY FOLLOW"
            status = "PRIVATE" if check.get("is_private") else check.get("profile_status", "AVAILABLE")
            evidence_url = candidate
            notes = (
                "Official company Instagram follows this exact full-name account; the direct "
                "profile name matches the independently verified LinkedIn identity."
            )
        elif follow and follow.get("result") == "NOT_FOUND_IN_COMPANY_FOLLOWING":
            method = "Official company Following list exact-name check"
            result = "NOT FOUND IN COMPANY FOLLOWING"
            status = "NOT VERIFIED"
            evidence_url = company_ig
            notes = "No exact full-name account was found in the official company account's Following list."
        elif follow and follow.get("result") == "COMPANY_ACCOUNT_NOT_CONFIRMED":
            method = "Company-account validation before Following-list check"
            result = "COMPANY ACCOUNT NOT CONFIRMED"
            status = "NOT VERIFIED"
            evidence_url = company_ig
            notes = "The supplied company Instagram did not clearly represent the company, so no identity inference was made."
        else:
            method = "SOP research queue from current-verified LinkedIn lead"
            result = "NO VERIFIED COMPANY INSTAGRAM SEARCH PATH"
            status = "NOT VERIFIED"
            evidence_url = company_ig
            notes = "No confirmed official company Instagram was available for the required company-relationship check."

        search_rows.append({
            "Company Name": seed.get("Company Name", ""),
            "Person Name": seed.get("Person Name", ""),
            "Current Job Title": seed.get("Current Job Title", ""),
            "Person LinkedIn URL": linkedin_url,
            "Company Instagram URL": company_ig,
            "Search Method": method,
            "Candidate Instagram URL": candidate,
            "Search Result": result,
            "Instagram Profile Status": status,
            "Identity Evidence URL": evidence_url,
            "Evidence Notes": notes,
        })

        if key in final_linkedin:
            continue
        gates = json.loads(db_profile.get("failed_gates") or "[]")
        if check and db_profile.get("verdict") == "RESEARCH FURTHER":
            reason = "Identity is verified, but the total qualification score remains below the SOP minimum of 90."
            review_status = "RESEARCH FURTHER"
        elif check:
            reason = ", ".join(gates) or "The candidate does not meet every Instagram FINAL gate."
            review_status = "NOT FINAL"
        elif follow and follow.get("result") == "NOT_FOUND_IN_COMPANY_FOLLOWING":
            reason = "No exact-name account was found through the official company Following-list evidence path."
            review_status = "NO VERIFIED PROFILE"
        elif follow and follow.get("result") == "COMPANY_ACCOUNT_NOT_CONFIRMED":
            reason = "Official company Instagram account could not be confirmed."
            review_status = "COMPANY ACCOUNT REVIEW"
        else:
            reason = "No verified personal Instagram and current-company relationship evidence was available."
            review_status = "NO VERIFIED SEARCH PATH"
        review_rows.append({
            "Company Name": seed.get("Company Name", ""),
            "Person Name": seed.get("Person Name", ""),
            "Current Job Title": seed.get("Current Job Title", ""),
            "Person LinkedIn URL": linkedin_url,
            "Company Instagram URL": company_ig,
            "Candidate Instagram URL": candidate,
            "Instagram Verification": db_profile.get("verification_status") or "UNVERIFIED",
            "Identity Score": db_profile.get("identity_score") if db_profile.get("identity_score") is not None else "",
            "Usability": db_profile.get("usability") or "",
            "Qualification Score": db_profile.get("total") if db_profile.get("total") is not None else "",
            "Verdict": db_profile.get("verdict") or "NOT SCORED",
            "Review Status": review_status,
            "Missing / Reason": reason,
        })

    payload = {
        "summary": {
            "researched_people": len(search_rows),
            "verified_company_follow_profiles": len(checks),
            "final_rows": len(final_rows),
            "review_rows": len(review_rows),
        },
        "final_columns": final_columns,
        "final_rows": final_rows,
        "search_columns": SEARCH_COLUMNS,
        "search_rows": search_rows,
        "review_columns": REVIEW_COLUMNS,
        "review_rows": review_rows,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**payload["summary"], "final_companies": len(per_company),
                      "max_per_company": max(per_company.values(), default=0),
                      "output": str(OUTPUT)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
