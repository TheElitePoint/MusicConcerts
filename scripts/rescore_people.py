"""Recalculate channel scores for explicitly supplied person IDs."""
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


def main(person_ids: list[int]) -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        for person_id in person_ids:
            facts = build_facts(ctx, person_id)
            if not facts:
                raise RuntimeError(f"person not found: {person_id}")
            for channel in ("linkedin", "instagram"):
                result = score(facts, channel)
                previous = ctx.db.one(
                    "SELECT why_company,why_person FROM scores WHERE person_id=? AND channel=?",
                    (person_id, channel),
                )
                ctx.db.x(
                    """INSERT OR REPLACE INTO scores
                       (person_id,channel,subscores,total,verdict,failed_gates,
                        why_company,why_person,sop_version,scored_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        person_id, channel, ctx.db.js(result.subscores), result.total,
                        result.verdict, ctx.db.js(result.failed_gates),
                        previous["why_company"] if previous else "",
                        previous["why_person"] if previous else "",
                        config.scoring()["sop_version"], utcnow(),
                    ),
                )
                print(f"person_id={person_id} channel={channel} total={result.total} verdict={result.verdict}")
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("person_ids", nargs="+", type=int)
    args = parser.parse_args()
    raise SystemExit(main(args.person_ids))
