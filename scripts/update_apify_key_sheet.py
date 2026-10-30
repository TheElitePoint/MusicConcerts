"""Maintain the "Apify Accounts" tab of the team's API-keys sheet.

* Adds any token the pipeline uses (.env APIFY_TOKENS) that the tab lacks, as a new
  row with the key in column D (email unknown -- the pipeline never had it).
* Rewrites ONE status column (the one headed "Live Status...") with a live check.
Columns are found BY HEADER NAME -- other people edit this tab too, and renamed
or reordered columns must not make this script write into the wrong one. Every
other column is never modified.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config  # noqa: E402

SPREADSHEET_ID = "1KC_Kjy4y5Ns383oXR3NncH8DtGntSMsMWdKpjITQ1Zc"
TAB = "Apify Accounts"
CREDENTIALS = "/Users/anirudhtyagi/Downloads/rosy-spring-507811-c2-79a06cc1bdba.json"


def status(token: str) -> str:
    try:
        r = requests.get("https://api.apify.com/v2/users/me/limits",
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if r.status_code == 401:
            return "dead - token not found (401)"
        d = r.json()["data"]
        cap = float(d["limits"]["maxMonthlyUsageUsd"] or 0)
        left = max(0.0, cap - float(d["current"]["monthlyUsageUsd"] or 0))
        end = d["monthlyUsageCycle"]["endAt"][:10]
        if left >= 0.10:
            return f"usable, ${left:.2f} left of ${cap:.0f} (resets {end})"
        return f"exhausted, resets {end}"
    except Exception as exc:                              # noqa: BLE001
        return f"check failed ({type(exc).__name__})"


def main() -> int:
    creds = Credentials.from_service_account_file(
        CREDENTIALS, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    api = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
    values = api.values().get(spreadsheetId=SPREADSHEET_ID,
                              range=f"'{TAB}'!A1:Z2000").execute().get("values", [])
    header = [h.strip().lower() for h in values[0]]
    try:
        key_col = header.index("api key")
        notes_col = next(i for i, h in enumerate(header) if h in ("notes", "status"))
        live_col = next(i for i, h in enumerate(header) if h.startswith(("live status", "status (live check")))
    except (ValueError, StopIteration):
        raise RuntimeError(f"unexpected header: {values[0]}")
    live_letter = chr(ord("A") + live_col)

    cell = lambda r, i: r[i].strip() if len(r) > i else ""  # noqa: E731
    in_sheet = {cell(r, key_col) for r in values[1:] if cell(r, key_col)}
    missing = [t for t in config.keys("apify") if t not in in_sheet]
    if missing:
        first = len(values) + 1
        rows = []
        for t in missing:
            row = [""] * (max(key_col, notes_col) + 1)
            row[notes_col], row[key_col] = "in pipeline .env (email unknown)", t
            rows.append(row)
        api.values().update(spreadsheetId=SPREADSHEET_ID, range=f"'{TAB}'!A{first}",
                            valueInputOption="RAW", body={"values": rows}).execute()
        values += rows

    def others(rows):
        return [[c for i, c in enumerate(r) if i != live_col and c] for r in rows]
    before = others(values)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    column = []
    for r in values[1:]:
        key = cell(r, key_col)
        column.append([f"{status(key)} (live check {stamp})" if key.startswith("apify_api_")
                       else cell(r, live_col)])
    api.values().update(spreadsheetId=SPREADSHEET_ID,
                        range=f"'{TAB}'!{live_letter}2:{live_letter}{len(column) + 1}",
                        valueInputOption="RAW", body={"values": column}).execute()

    after = api.values().get(spreadsheetId=SPREADSHEET_ID,
                             range=f"'{TAB}'!A1:Z2000").execute().get("values", [])
    if others(after) != before:
        raise RuntimeError("a column other than the live-status column changed")
    usable = [c[0] for c in column if c[0].startswith("usable")]
    print(f"added {len(missing)} keys; checked {sum(1 for c in column[1:] if c[0])}; usable {len(usable)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
