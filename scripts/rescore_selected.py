"""Rescore selected people without regenerating narrative text.

Used for narrow evidence corrections after the full deterministic score pass. Existing
SOP rationale text is retained; only structured subscores, verdicts and gate results are
recomputed from the database facts.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config  # noqa: E402
from mp.db import utcnow  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.sop.scoring import score  # noqa: E402
from mp.stages.s8_score import build_facts  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("person_ids", nargs="+", type=int)
    args = parser.parse_args()

    ctx = Context.create(db_path="data/master.sqlite")
    sop_version = config.scoring()["sop_version"]
    try:
        for person_id in args.person_ids:
            facts = build_facts(ctx, person_id)
            if not facts:
                continue
            for channel in ("linkedin", "instagram"):
                result = score(facts, channel)
                old = ctx.db.one(
                    "SELECT why_company,why_person FROM scores WHERE person_id=? AND channel=?",
                    (person_id, channel),
                )
                ctx.db.x(
                    """INSERT OR REPLACE INTO scores
                       (person_id,channel,subscores,total,verdict,failed_gates,
                        why_company,why_person,sop_version,scored_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        person_id,
                        channel,
                        ctx.db.js(result.subscores),
                        result.total,
                        result.verdict,
                        ctx.db.js(result.failed_gates),
                        old["why_company"] if old else "",
                        old["why_person"] if old else "",
                        sop_version,
                        utcnow(),
                    ),
                )
                print(person_id, channel, result.total, result.verdict)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
