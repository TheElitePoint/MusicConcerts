"""Read the existing Music Data Google Sheet before any update."""
from __future__ import annotations

import json
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SPREADSHEET_ID = "1OihYVSE8SGM2nKiBqyXK4Aps1jz6NrKcfTpdrgmyRJc"
CREDENTIALS = "/Users/anirudhtyagi/Downloads/rosy-spring-507811-c2-79a06cc1bdba.json"
OUTPUT = Path("data/reference/music_data_google_sheet_snapshot_20260907.json")


def main() -> int:
    creds = Credentials.from_service_account_file(
        CREDENTIALS, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    meta = service.spreadsheets().get(
        spreadsheetId=SPREADSHEET_ID,
        fields="properties.title,sheets.properties",
    ).execute()
    tabs = {}
    for tab in ("LinkedIn", "Instagram"):
        values = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{tab}'!A1:AC2000",
        ).execute().get("values", [])
        tabs[tab] = values
    payload = {"metadata": meta, "tabs": tabs}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for tab, rows in tabs.items():
        print(json.dumps({
            "tab": tab,
            "rows_read": len(rows),
            "header": rows[0] if rows else [],
            "last_nonempty_row": len(rows),
        }, ensure_ascii=False))
    print(json.dumps({"snapshot": str(OUTPUT), "spreadsheet_title": meta["properties"]["title"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
