"""Reject exact person/company collisions found in the additional LinkedIn audit.

These IDs were individually reviewed against the target company's official domain or
LinkedIn organisation. They are not rejected merely for an unresolved result: each is a
positive collision, wrong organisation, or materially implausible self-claim.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.pipeline import Context  # noqa: E402


REJECTS = {
    # Generic-name collisions: none belongs to the target music company.
    **{pid: "Wrong employer: generic Legacy name collision, not Legacy Records." for pid in (875, 877, 878, 879, 882, 883, 884, 885, 886)},
    **{pid: "Wrong employer: DDS name collision, not the target music label." for pid in (796, 802, 806, 807, 810, 813)},
    **{pid: "Wrong employer: Modern Love name collision, not the target record label." for pid in (926, 927, 929, 931, 932)},
    **{pid: "Wrong employer: Technicolour name collision, not the target record label." for pid in (890, 891, 892)},
    # Exact alternative organisations or clearly different businesses.
    1443: "Wrong employer: Gamma telecom, not gamma. music.",
    1444: "Wrong employer: Gamma telecom, not gamma. music.",
    1447: "Wrong employer: Gamma telecom, not gamma. music.",
    1179: "Wrong employer: Ubiquity BPO, not Ubiquity Records.",
    1180: "Wrong employer: Ubiquity Management, not Ubiquity Records.",
    1181: "Wrong employer: unrelated Ubiquity company, not Ubiquity Records.",
    1208: "Wrong employer: Ubiquity Group, not Ubiquity Records.",
    846: "Wrong employer: unrelated Kscope company, not the target label.",
    2244: "Wrong employer: Apollo Sound production-sound business, not the target music company.",
    820: "Wrong employer: FLG Partners, not the target music company.",
    823: "Wrong employer: FLG Partners, not the target music company.",
    1483: "Wrong employer: unrelated Trisol business, not the target music company.",
    1484: "Wrong employer: unrelated Trisol business, not the target music company.",
    1781: "Wrong employer: FOUR Music Inc, not Sony's Four Music label.",
    2202: "Wrong employer: different Swing Entertainment organisation.",
    1092: "Wrong employer: Signature Sounds Entertainment, not Signature Sounds Recordings.",
    1084: "Wrong employer: Northern Spy Productions, not Northern Spy Records.",
    1544: "Wrong employer: personal Tompkins-name collision, not Tompkins Square label.",
    2017: "Current OVO Sound employment could not be corroborated; generic company-owner self-claim.",
    1832: "Wrong employer: GRAPPA doo/person-name collision, not Norway's Grappa label.",
    2137: "Materially implausible CUBE Entertainment CEO self-claim; conflicts with target leadership and geography.",
    1898: "RK Music identity could not be tied to the qualified target company/domain.",
}


def main() -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    rejected = 0
    try:
        for person_id, reason in REJECTS.items():
            row = ctx.db.one("SELECT person_id FROM people WHERE person_id=?", (person_id,))
            if not row:
                continue
            ctx.db.x(
                """UPDATE people SET status='REJECTED',reject_reason=?,current_verified=0,
                   last_researched=? WHERE person_id=?""",
                (reason, utcnow(), person_id),
            )
            ctx.db.x(
                """UPDATE profiles SET verification_status='REJECTED',verified_at=?
                   WHERE person_id=? AND channel='linkedin'""",
                (utcnow(), person_id),
            )
            rejected += 1
        print(f"rejected={rejected}")
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
