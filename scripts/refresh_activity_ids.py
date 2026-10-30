"""Refresh company activity evidence for explicitly supplied company IDs (free sources)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mp.pipeline import Context                       # noqa: E402
from mp.stages import s3_verify                       # noqa: E402


def main(company_ids: list[int]) -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        marks = ",".join("?" * len(company_ids))
        rows = ctx.db.q(f"""SELECT company_id, canonical_name, website, domain, mbid, discogs_id
                            FROM companies WHERE status='QUALIFIED' AND website IS NOT NULL
                              AND company_id IN ({marks})""", tuple(company_ids))
        done = 0
        for row in rows:
            try:
                s3_verify.verify_one(ctx, row)
                done += 1
                print(f"  {done}/{len(rows)} {row['canonical_name'][:34]}", flush=True)
            except Exception as exc:
                print(f"  skip {row['canonical_name'][:30]}: {type(exc).__name__}", flush=True)
            # verify_one sets status back to VERIFIED; these already cleared the gates.
            ctx.db.x("UPDATE companies SET status='QUALIFIED' WHERE company_id=? AND status='VERIFIED'",
                     (row["company_id"],))
        print(f"refreshed {done} companies", flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main([int(a) for a in sys.argv[1:]]))
