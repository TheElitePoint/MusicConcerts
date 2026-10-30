"""Run the person half of the pipeline: S5 -> S6 -> S7 -> S8 -> S9."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mp.pipeline import Context                                   # noqa: E402
from mp.sources import websearch                                  # noqa: E402
from mp.stages import (s5_people, s6_profiles, s7_person_verify,  # noqa: E402
                       s8_score, s9_export)


def main() -> int:
    ctx = Context.create(countries=["GB", "US", "IE", "AU"], db_path="data/master.sqlite")
    try:
        for name, fn in (("people", s5_people.run), ("profiles", s6_profiles.run),
                         ("verify-people", s7_person_verify.run), ("score", s8_score.run)):
            print(f"{name:14s} {fn(ctx)}", flush=True)
        print(f"{'export':14s} {s9_export.run(ctx)}", flush=True)
        print(f"\noutputs: {ctx.run_dir}", flush=True)
        print(f"search backends: {websearch.backend_status()}", flush=True)
        print(f"apify ring: {ctx.ledger.keyring.summary().get('apify')}", flush=True)
        for p, s in ctx.ledger.summary().items():
            print(f"   {p:12s} calls={s['calls']:<4d} cache={s['cache_hits']:<4d} "
                  f"cost=${s['cost']:.3f}{'  TRIPPED' if s['tripped'] else ''}", flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
