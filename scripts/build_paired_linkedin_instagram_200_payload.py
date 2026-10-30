"""Create one-to-one LinkedIn and Instagram rows for the same 200 people."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.stages.s8_score import build_facts  # noqa: E402
from mp.stages.s9_export import _mandatory_ok, _row_for  # noqa: E402

SOURCE = ROOT / "data/instagram_200_delivery_20260907.json"
OUTPUT = ROOT / "data/paired_linkedin_instagram_200_20260907.json"


def key(value: str | None) -> str:
    return (value or "").strip().lower().rstrip("/")


def main() -> int:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    instagram_rows = source["final_rows"] + source["review_rows"]
    sop = config.sop()
    linkedin_columns = sop["sheets"]["linkedin"]["columns"]
    linkedin_mandatory = sop["sheets"]["linkedin"]["mandatory"]
    not_found = sop["sheets"]["not_found_literal"]
    ctx = Context.create(db_path="data/master.sqlite")
    linkedin_rows: list[dict] = []
    try:
        index = {
            key(row["linkedin_url"]): int(row["person_id"])
            for row in ctx.db.q(
                "SELECT person_id,url AS linkedin_url FROM profiles WHERE channel='linkedin'"
            )
        }
        scores = {
            int(row["person_id"]): row
            for row in ctx.db.q("SELECT * FROM scores WHERE channel='linkedin'")
        }
        for ig_row in instagram_rows:
            linkedin_url = key(ig_row.get("Person LinkedIn URL"))
            person_id = index.get(linkedin_url)
            score = scores.get(person_id or -1)
            if not person_id or not score or score["verdict"] != "FINAL PASS" or int(score["total"]) < 90:
                raise RuntimeError(f"LinkedIn FINAL record unavailable for {ig_row.get('Person Name')}")
            facts = build_facts(ctx, person_id)
            row = _row_for(facts, score, "linkedin", sop)
            # Preserve the Instagram candidate selected for the matched cohort.
            row["Person Instagram URL"] = ig_row.get("Person Instagram URL") or not_found
            out = {column: row.get(column, "") for column in linkedin_columns}
            ok, missing = _mandatory_ok(out, linkedin_mandatory, not_found)
            if not ok:
                raise RuntimeError(f"Missing LinkedIn mandatory fields for {ig_row.get('Person Name')}: {missing}")
            linkedin_rows.append(out)
    finally:
        ctx.close()

    if len(linkedin_rows) != 200 or len(instagram_rows) != 200:
        raise RuntimeError("Paired cohort is not exactly 200 rows per channel")
    for position, (li, ig) in enumerate(zip(linkedin_rows, instagram_rows), start=1):
        if key(li.get("Person LinkedIn URL")) != key(ig.get("Person LinkedIn URL")):
            raise RuntimeError(f"Row {position} person mismatch")

    payload = {
        "summary": {
            "paired_people": 200,
            "linkedin_final": 200,
            "instagram_final": len(source["final_rows"]),
            "instagram_review": len(source["review_rows"]),
        },
        "linkedin_columns": linkedin_columns,
        "instagram_columns": source["columns"],
        "linkedin_rows": linkedin_rows,
        "instagram_rows": instagram_rows,
        "instagram_final_rows": source["final_rows"],
        "instagram_review_rows": source["review_rows"],
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**payload["summary"], "output": str(OUTPUT)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
