"""Audit red-formatted Instagram rows in the live Music Data Google Sheet."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SPREADSHEET_ID = "1OihYVSE8SGM2nKiBqyXK4Aps1jz6NrKcfTpdrgmyRJc"
CREDENTIALS = "/Users/anirudhtyagi/Downloads/rosy-spring-507811-c2-79a06cc1bdba.json"
OUTPUT_DIR = Path("data/instagram_runs")


def _rgb(color: dict | None) -> tuple[float, float, float]:
    color = color or {}
    return (
        float(color.get("red", 0)),
        float(color.get("green", 0)),
        float(color.get("blue", 0)),
    )


def _format_color(fmt: dict, base: str) -> dict | None:
    direct = fmt.get(base)
    if direct:
        return direct
    return fmt.get(f"{base}Style", {}).get("rgbColor")


def _is_red(color: dict | None) -> bool:
    red, green, blue = _rgb(color)
    return red >= 0.70 and red >= green + 0.20 and red >= blue + 0.20


def _cell_value(cell: dict) -> object:
    value = cell.get("effectiveValue", {})
    for key in ("stringValue", "numberValue", "boolValue", "formulaValue"):
        if key in value:
            return value[key]
    return ""


def main() -> int:
    creds = Credentials.from_service_account_file(
        CREDENTIALS, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
    payload = sheets.get(
        spreadsheetId=SPREADSHEET_ID,
        ranges=["'Instagram'!A1:AC2000", "'LinkedIn'!A1:AC2000"],
        includeGridData=True,
    ).execute()
    findings = []
    rows_examined = {}
    for sheet in payload["sheets"]:
        title = sheet["properties"]["title"]
        if title not in {"Instagram", "LinkedIn"}:
            continue
        row_data = sheet.get("data", [{}])[0].get("rowData", [])
        rows_examined[title] = max(len(row_data) - 1, 0)
        headers = [str(_cell_value(c)) for c in (row_data[0].get("values", []) if row_data else [])]
        for row_index, row in enumerate(row_data[1:], start=2):
            cells = row.get("values", [])
            red_cells = []
            values = {}
            for col_index, cell in enumerate(cells):
                header = headers[col_index] if col_index < len(headers) else f"Column {col_index + 1}"
                values[header] = _cell_value(cell)
                effective = cell.get("effectiveFormat", {})
                entered = cell.get("userEnteredFormat", {})
                colors = {
                    "effective_background": _format_color(effective, "backgroundColor"),
                    "effective_font": _format_color(effective.get("textFormat", {}), "foregroundColor"),
                    "entered_background": _format_color(entered, "backgroundColor"),
                    "entered_font": _format_color(entered.get("textFormat", {}), "foregroundColor"),
                }
                red_kinds = [kind for kind, color in colors.items() if _is_red(color)]
                if red_kinds:
                    red_cells.append({"column": header, "column_index": col_index + 1, "red_kinds": red_kinds})
            if red_cells:
                findings.append({
                    "sheet": title,
                    "sheet_row": row_index,
                    "person_name": values.get("Person Name", ""),
                    "company_name": values.get("Company Name", ""),
                    "person_instagram_url": values.get("Person Instagram URL", ""),
                    "verification_status": values.get("Instagram Profile Status", "") or values.get("LinkedIn Profile Status", ""),
                    "qualification_score": values.get("Qualification Score", ""),
                    "why_this_person_fits": values.get("Why This Person Fits", ""),
                    "red_cells": red_cells,
                })
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output = OUTPUT_DIR / f"red_sheet_audit_{stamp}.json"
    result = {
        "spreadsheet_title": payload["properties"]["title"],
        "sheets": ["Instagram", "LinkedIn"],
        "rows_examined": rows_examined,
        "red_rows": findings,
    }
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
