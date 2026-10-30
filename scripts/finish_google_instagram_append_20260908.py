"""Finish formatting and tracking fields for the two 2026-09-08 Instagram appends."""
from __future__ import annotations

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


SPREADSHEET_ID = "1OihYVSE8SGM2nKiBqyXK4Aps1jz6NrKcfTpdrgmyRJc"
CREDENTIALS = "/Users/anirudhtyagi/Downloads/rosy-spring-507811-c2-79a06cc1bdba.json"
TAB = "Instagram"
FIRST_ROW = 64
LAST_ROW = 64


def main() -> int:
    creds = Credentials.from_service_account_file(
        CREDENTIALS, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    spreadsheet = service.spreadsheets()
    meta = spreadsheet.get(
        spreadsheetId=SPREADSHEET_ID, fields="sheets.properties"
    ).execute()
    sheet_id = next(
        s["properties"]["sheetId"]
        for s in meta["sheets"]
        if s["properties"]["title"] == TAB
    )

    # Preserve the established visual style from the last pre-existing row.
    spreadsheet.batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "requests": [
                {
                    "copyPaste": {
                        "source": {
                            "sheetId": sheet_id,
                            "startRowIndex": FIRST_ROW - 2,
                            "endRowIndex": FIRST_ROW - 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 29,
                        },
                        "destination": {
                            "sheetId": sheet_id,
                            "startRowIndex": FIRST_ROW - 1,
                            "endRowIndex": LAST_ROW,
                            "startColumnIndex": 0,
                            "endColumnIndex": 29,
                        },
                        "pasteType": "PASTE_FORMAT",
                        "pasteOrientation": "NORMAL",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": FIRST_ROW - 1,
                            "endRowIndex": LAST_ROW,
                            "startColumnIndex": 0,
                            "endColumnIndex": 29,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "verticalAlignment": "TOP",
                                "wrapStrategy": "WRAP",
                            }
                        },
                        "fields": "userEnteredFormat.verticalAlignment,userEnteredFormat.wrapStrategy",
                    }
                },
            ]
        },
    ).execute()

    # Populate the two tracking columns used by the existing Instagram tab.
    spreadsheet.values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "valueInputOption": "USER_ENTERED",
            "data": [
                {
                    "range": f"'{TAB}'!T{FIRST_ROW}:T{LAST_ROW}",
                    "values": [["SOP FINAL"] for _ in range(FIRST_ROW, LAST_ROW + 1)],
                },
                {
                    "range": f"'{TAB}'!AC{FIRST_ROW}:AC{LAST_ROW}",
                    "values": [["2026-09-08"] for _ in range(FIRST_ROW, LAST_ROW + 1)],
                },
            ],
        },
    ).execute()
    print(f"formatted={FIRST_ROW}:{LAST_ROW} tracking_fields_updated=2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
