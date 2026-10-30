"""Remove a false Instagram identity match caught by signed-in browser review."""
from __future__ import annotations

import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    try:
        cur = con.execute(
            """UPDATE profiles
               SET verification_status='UNVERIFIED', identity_score=0,
                   usability='none', match_signals=?, verified_at=NULL
               WHERE person_id=392 AND channel='instagram'
                 AND url='https://www.instagram.com/intrada_nt/'""",
            ('{"false_match_reason":"Real-estate account; unrelated to the music label Intrada"}',),
        )
        con.commit()
    finally:
        con.close()
    print(f"corrected={cur.rowcount}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
