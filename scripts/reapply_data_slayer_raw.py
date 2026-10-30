"""Reapply an already-downloaded Data Slayer dataset to selected candidates."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from verify_profiles_apimaestro import _apply, _candidates
from verify_profiles_data_slayer import _flatten


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("raw_json")
    parser.add_argument("--ids-json", required=True)
    args = parser.parse_args()

    payload = json.loads(Path(args.ids_json).read_text(encoding="utf-8"))
    allowed = {int(row["person_id"]) for row in payload.get("rows", [])}
    items = json.loads(Path(args.raw_json).read_text(encoding="utf-8"))

    con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
    con.row_factory = sqlite3.Row
    try:
        rows = [row for row in _candidates(con, 0) if int(row["person_id"]) in allowed]
        stats = _apply(con, rows, _flatten(items))
    finally:
        con.close()
    print(f"candidates={len(rows)} profiles={len(_flatten(items))}")
    print(" ".join(f"{key}={value}" for key, value in stats.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
