"""Re-verify, re-score and export every person at a qualified company.

Needed because S5 can add candidates after S7 has already run in the same chain,
leaving the newest people unverified and therefore unscored. All inputs are cached,
so this costs nothing.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mp.pipeline import Context                                    # noqa: E402
from mp.stages import s7_person_verify, s8_score, s9_export        # noqa: E402


def main() -> int:
    ctx = Context.create(countries=["GB", "US", "IE", "AU"], db_path="data/master.sqlite")
    try:
        print("verify:", s7_person_verify.run(ctx), flush=True)
        print("score :", s8_score.run(ctx), flush=True)
        print("export:", s9_export.run(ctx), flush=True)
        print("run dir:", ctx.run_dir, flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
