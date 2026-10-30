"""Refresh company activity evidence for companies holding near-miss people.

SS45 calls 85-89 "RESEARCH FURTHER -- resolve missing information", not a rejection.
These people lose points on SS41 B1/B2 (activity recency and distinct signal
categories) and SS43 D2 (activity evidence), all COMPANY-side facts that free
sources can refresh. Re-verifying those companies is the cheapest way to convert
already-verified decision-makers into final rows.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mp.pipeline import Context                       # noqa: E402
from mp.stages import s3_verify                       # noqa: E402


def main() -> int:
    ctx = Context.create(countries=["GB", "US", "IE", "AU"], db_path="data/master.sqlite")
    try:
        rows = ctx.db.q("""SELECT DISTINCT c.company_id, c.canonical_name, c.website,
                                  c.domain, c.mbid, c.discogs_id
                           FROM companies c
                           JOIN people p ON p.company_id = c.company_id
                           JOIN scores s ON s.person_id = p.person_id
                                        AND s.channel='linkedin'
                           WHERE c.status='QUALIFIED'
                             AND s.verdict='RESEARCH FURTHER'
                             AND c.website IS NOT NULL""")
        print(f"refreshing activity for {len(rows)} companies holding near-miss people",
              flush=True)
        done = 0
        for row in rows:
            try:
                s3_verify.verify_one(ctx, row)
                done += 1
                print(f"  {done}/{len(rows)} {row['canonical_name'][:34]}", flush=True)
            except Exception as exc:
                print(f"  skip {row['canonical_name'][:30]}: {type(exc).__name__}",
                      flush=True)
        # verify_one sets status back to VERIFIED; restore QUALIFIED so the people
        # remain eligible (these companies already cleared all eight gates).
        ids = ",".join(str(r["company_id"]) for r in rows)
        if ids:
            ctx.db.x(f"UPDATE companies SET status='QUALIFIED' "
                     f"WHERE company_id IN ({ids}) AND status='VERIFIED'")
        print(f"refreshed {done} companies", flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
