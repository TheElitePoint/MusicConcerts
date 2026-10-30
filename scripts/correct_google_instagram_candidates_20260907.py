"""Remove unsupported appended Instagram candidates from the main tab.

The removed rows are preserved in a separate audit tab with explicit identity
decisions and reasons. The script verifies the exact source block before edits.
"""
from __future__ import annotations

import json
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SPREADSHEET_ID = "1OihYVSE8SGM2nKiBqyXK4Aps1jz6NrKcfTpdrgmyRJc"
CREDENTIALS = "/Users/anirudhtyagi/Downloads/rosy-spring-507811-c2-79a06cc1bdba.json"
AUDIT_SOURCE = Path("data/instagram_identity_audit_20260907.json")
BACKUP = Path("data/google_instagram_appended_rows_backup_20260907.json")
RESULT = Path("data/google_instagram_correction_20260907.json")
AUDIT_TAB = "Instagram Candidate Audit"


def key(value: str | None) -> str:
    return (value or "").strip().lower().rstrip("/")


def name_company(row: dict) -> tuple[str, str]:
    return key(row.get("Person Name")), key(row.get("Company Name"))


def main() -> int:
    creds = Credentials.from_service_account_file(
        CREDENTIALS, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    api = service.spreadsheets()
    meta = api.get(
        spreadsheetId=SPREADSHEET_ID, fields="properties.title,sheets.properties"
    ).execute()
    props = {s["properties"]["title"]: s["properties"] for s in meta["sheets"]}
    if "Instagram" not in props:
        raise RuntimeError("Instagram tab is missing")
    if AUDIT_TAB in props:
        raise RuntimeError(f"{AUDIT_TAB} already exists; refusing to overwrite it")

    values = api.values().get(
        spreadsheetId=SPREADSHEET_ID, range="'Instagram'!A1:AC2000"
    ).execute().get("values", [])
    if len(values) != 266:
        raise RuntimeError(f"Expected 266 populated rows before correction, found {len(values)}")
    header = values[0]
    appended_values = values[102:266]  # rows 103:266, the exact prior write
    appended = [dict(zip(header, row)) for row in appended_values]

    audit = json.loads(AUDIT_SOURCE.read_text(encoding="utf-8"))["rows"]
    audit_by_person = {name_company(row): row for row in audit}
    decisions = [audit_by_person.get(name_company(row), {}) for row in appended]
    if any(not row for row in decisions):
        raise RuntimeError("One or more appended rows could not be matched to the identity audit")
    keep = [(row, decision) for row, decision in zip(appended, decisions) if decision["Decision"] == "KEEP FINAL"]
    remove = [(row, decision) for row, decision in zip(appended, decisions) if decision["Decision"] != "KEEP FINAL"]
    if len(keep) != 4 or len(remove) != 160:
        raise RuntimeError(f"Expected 4 keep and 160 remove; found {len(keep)} and {len(remove)}")
    if [decision["Decision"] for decision in decisions[:4]] != ["KEEP FINAL"] * 4:
        raise RuntimeError("The four verified rows are not the first four rows of the appended block")

    BACKUP.write_text(json.dumps({"header": header, "rows": appended_values}, ensure_ascii=False, indent=2), encoding="utf-8")

    audit_columns = header + [
        "Identity Audit Decision", "Identity Audit Reason", "Audited IG Display Name",
        "Audited IG Bio", "Name Exact", "Name Similarity", "Company Signal",
        "Company Domain Signal", "Stored Verification",
    ]
    audit_rows = []
    for source_row, decision in remove:
        audit_rows.append([source_row.get(column, "") for column in header] + [
            decision["Decision"], decision["Reason"], decision["IG Display Name"],
            decision["IG Bio"], decision["Name Exact"], decision["Name Similarity"],
            decision["Company Signal"], decision["Company Domain Signal"],
            decision["Stored Verification"],
        ])

    add = api.batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{
            "addSheet": {"properties": {
                "title": AUDIT_TAB,
                "gridProperties": {"rowCount": 400, "columnCount": len(audit_columns), "frozenRowCount": 1},
                "tabColorStyle": {"rgbColor": {"red": 0.96, "green": 0.62, "blue": 0.04}},
            }}
        }]},
    ).execute()
    audit_sheet_id = add["replies"][0]["addSheet"]["properties"]["sheetId"]

    api.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{AUDIT_TAB}'!A1",
        valueInputOption="RAW",
        body={"values": [audit_columns] + audit_rows},
    ).execute()
    # Remove only the 160 unsupported rows from the main Instagram tab. The four
    # verified rows at 103:106 remain. Clearing contents is recoverable and does
    # not disturb existing sheet structure or formatting.
    api.values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range="'Instagram'!A107:AC266",
        body={},
    ).execute()

    header_color = {"red": 0.06, "green": 0.46, "blue": 0.43}
    white = {"red": 1.0, "green": 1.0, "blue": 1.0}
    requests = [
        {
            "repeatCell": {
                "range": {"sheetId": audit_sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(audit_columns)},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": header_color,
                    "textFormat": {"bold": True, "foregroundColor": white},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": audit_sheet_id, "startRowIndex": 1, "endRowIndex": 161, "startColumnIndex": 0, "endColumnIndex": len(audit_columns)},
                "cell": {"userEnteredFormat": {"verticalAlignment": "TOP", "wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat.verticalAlignment,userEnteredFormat.wrapStrategy",
            }
        },
        {
            "setBasicFilter": {
                "filter": {"range": {"sheetId": audit_sheet_id, "startRowIndex": 0, "endRowIndex": 161, "startColumnIndex": 0, "endColumnIndex": len(audit_columns)}}
            }
        },
        {
            "autoResizeDimensions": {
                "dimensions": {"sheetId": audit_sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": len(audit_columns)}
            }
        },
    ]
    decision_col = len(header)
    requests.extend([
        {
            "addConditionalFormatRule": {
                "index": 0,
                "rule": {
                    "ranges": [{"sheetId": audit_sheet_id, "startRowIndex": 1, "endRowIndex": 161, "startColumnIndex": decision_col, "endColumnIndex": decision_col + 1}],
                    "booleanRule": {
                        "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": "REJECT CANDIDATE"}]},
                        "format": {"backgroundColor": {"red": 0.99, "green": 0.89, "blue": 0.89}, "textFormat": {"bold": True, "foregroundColor": {"red": 0.60, "green": 0.10, "blue": 0.10}}},
                    },
                },
            }
        },
        {
            "addConditionalFormatRule": {
                "index": 0,
                "rule": {
                    "ranges": [{"sheetId": audit_sheet_id, "startRowIndex": 1, "endRowIndex": 161, "startColumnIndex": decision_col, "endColumnIndex": decision_col + 1}],
                    "booleanRule": {
                        "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": "UNRESOLVED"}]},
                        "format": {"backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.78}, "textFormat": {"bold": True, "foregroundColor": {"red": 0.57, "green": 0.25, "blue": 0.05}}},
                    },
                },
            }
        },
    ])
    api.batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": requests}).execute()

    result = {
        "spreadsheet": meta["properties"]["title"],
        "main_instagram_rows_kept_from_append": 4,
        "main_instagram_rows_removed": 160,
        "audit_tab": AUDIT_TAB,
        "audit_unresolved": sum(d["Decision"] == "UNRESOLVED" for d in decisions),
        "audit_rejected": sum(d["Decision"] == "REJECT CANDIDATE" for d in decisions),
        "backup": str(BACKUP),
    }
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
