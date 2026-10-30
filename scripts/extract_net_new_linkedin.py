"""Extract the auditable net-new LinkedIn FINAL PASS cohort for workbook authoring."""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config  # noqa: E402
from mp.normalize import linkedin_person_url  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.stages.s8_score import build_facts  # noqa: E402
from mp.stages.s9_export import _mandatory_ok, _row_for  # noqa: E402

PRIOR_DELIVERY = ROOT / "data" / "net_new_linkedin_final.json"
OUTPUT = ROOT / "data" / "additional_100_linkedin_final_20260829.json"


def historical_urls() -> set[str]:
    out: set[str] = set()
    for path in ROOT.glob("runs/*/linkedin_final.csv"):
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                url = linkedin_person_url(row.get("Person LinkedIn URL", ""))
                if url:
                    out.add(url.lower())
    if PRIOR_DELIVERY.exists():
        payload = json.loads(PRIOR_DELIVERY.read_text(encoding="utf-8"))
        for row in payload.get("rows", []):
            url = linkedin_person_url(row.get("Person LinkedIn URL", ""))
            if url:
                out.add(url.lower())
    return out


def main() -> int:
    sop = config.sop()
    columns = sop["sheets"]["linkedin"]["columns"]
    mandatory = sop["sheets"]["linkedin"]["mandatory"]
    not_found = sop["sheets"]["not_found_literal"]
    old = historical_urls()
    seen: set[str] = set()
    per_company: dict[int, int] = defaultdict(int)
    rows_out: list[dict] = []
    blocked = historical = duplicates = capped = 0

    ctx = Context.create(db_path="data/master.sqlite")
    try:
        score_rows = ctx.db.q(
            """SELECT s.*,p.company_id
                 FROM scores s
                 JOIN people p USING(person_id)
                 JOIN companies c USING(company_id)
                 JOIN profiles pr ON pr.person_id=p.person_id AND pr.channel='linkedin'
                WHERE s.channel='linkedin' AND s.verdict='FINAL PASS'
                  AND p.status!='REJECTED' AND c.status='QUALIFIED'
                  AND pr.verification_status='VERIFIED'
                ORDER BY s.total DESC,c.canonical_name,p.full_name"""
        )
        for score_row in score_rows:
            facts = build_facts(ctx, score_row["person_id"])
            if not facts:
                continue
            url = linkedin_person_url(facts.get("linkedin_url", ""))
            if not url:
                blocked += 1
                continue
            key = url.lower()
            if key in old:
                historical += 1
                continue
            if key in seen:
                duplicates += 1
                continue
            company_id = int(facts["company_id"])
            if per_company[company_id] >= 10:
                capped += 1
                continue
            row = _row_for(facts, score_row, "linkedin", sop)
            ok, _missing = _mandatory_ok(row, mandatory, not_found)
            if not ok:
                blocked += 1
                continue
            rows_out.append({column: row.get(column, "") for column in columns})
            seen.add(key)
            per_company[company_id] += 1
    finally:
        ctx.close()

    OUTPUT.write_text(
        json.dumps({"columns": columns, "rows": rows_out}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "rows": len(rows_out), "companies": len(per_company),
        "historical_excluded": historical, "duplicates": duplicates,
        "capped": capped, "blocked_mandatory": blocked,
        "max_per_company": max(per_company.values(), default=0),
        "output": str(OUTPUT),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
