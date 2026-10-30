"""Re-run the SS29 gates over companies sitting in VERIFIED, and report movement.

`s3_verify.verify_one` always leaves a company in VERIFIED -- verification and
qualification are separate stages, so anything re-verified drops out of QUALIFIED
until the gates run again. Any script that re-verifies must therefore re-gate, or
it silently removes companies (and their already-exported people) from the sheet.

Prints what actually moved, because a re-gate that quietly demotes a company is
exactly the failure this script exists to make visible.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mp.db import utcnow                                   # noqa: E402
from mp.pipeline import Context                            # noqa: E402
from mp.sop.gates import evaluate                          # noqa: E402
from mp.stages.s4_gates import build_facts                 # noqa: E402

STATUS = {"PASS": "QUALIFIED", "REVIEW": "REVIEW", "REJECT": "REJECTED"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", default="VERIFIED")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    ctx = Context.create(db_path="data/master.sqlite")
    try:
        sts = tuple(s.strip() for s in args.status.split(","))
        rows = ctx.db.q(f"""SELECT company_id, canonical_name, status,
                              (SELECT COUNT(*) FROM people p
                                WHERE p.company_id = companies.company_id
                                  AND p.status='VERIFIED') AS people
                            FROM companies
                            WHERE status IN ({','.join('?' * len(sts))})
                            ORDER BY company_id""", sts)
        if args.limit:
            rows = rows[:args.limit]
        print(f"re-gating {len(rows)} companies", flush=True)

        moved = {"QUALIFIED": 0, "REVIEW": 0, "REJECTED": 0}
        lost_people = 0
        for row in rows:
            cid = row["company_id"]
            facts = build_facts(ctx, cid)
            if not facts:
                continue
            report = evaluate(facts)
            new = STATUS[report.verdict]
            ctx.db.x("""UPDATE companies SET gate_result=?, status=?, last_researched=?
                        WHERE company_id=?""",
                     (ctx.db.js(report.as_dict()), new, utcnow(), cid))
            moved[new] += 1
            if new != "QUALIFIED" and row["people"]:
                lost_people += row["people"]
                blocking = [g.id for g in report.gates if g.result != "PASS"]
                print(f"  DEMOTED {row['canonical_name'][:30]:32s} -> {new} "
                      f"({row['people']} verified people affected) {blocking}", flush=True)

        print(f"\n{moved}")
        if lost_people:
            print(f"WARNING: {lost_people} verified people are at companies that no "
                  f"longer qualify and will drop out of the sheet")
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
