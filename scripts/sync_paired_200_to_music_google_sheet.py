"""Append the missing members of the matched 200-person cohort to Instagram.

The LinkedIn tab already contains all 200 people. This script reads both tabs
again immediately before writing, de-duplicates by person+company and channel
URL, then appends only missing Instagram people in the tab's existing order.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SPREADSHEET_ID = "1OihYVSE8SGM2nKiBqyXK4Aps1jz6NrKcfTpdrgmyRJc"
CREDENTIALS = "/Users/anirudhtyagi/Downloads/rosy-spring-507811-c2-79a06cc1bdba.json"
PAYLOAD = Path("data/paired_linkedin_instagram_200_20260907.json")
AUDIT = Path("data/google_sheet_sync_paired_200_20260907.json")


def key(value: str | None) -> str:
    return (value or "").strip().lower().rstrip("/")


def name_company(row: dict) -> tuple[str, str]:
    return key(row.get("Person Name")), key(row.get("Company Name"))


def a1_row_range(value: str) -> tuple[int, int]:
    match = re.search(r"![A-Z]+(\d+):[A-Z]+(\d+)$", value)
    if not match:
        raise RuntimeError(f"Unexpected updatedRange: {value}")
    return int(match.group(1)), int(match.group(2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    creds = Credentials.from_service_account_file(
        CREDENTIALS, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    sheets_api = service.spreadsheets()
    meta = sheets_api.get(
        spreadsheetId=SPREADSHEET_ID, fields="properties.title,sheets.properties"
    ).execute()
    properties = {s["properties"]["title"]: s["properties"] for s in meta["sheets"]}
    if "LinkedIn" not in properties or "Instagram" not in properties:
        raise RuntimeError("Required LinkedIn and Instagram tabs are missing")

    live = {}
    for tab in ("LinkedIn", "Instagram"):
        live[tab] = sheets_api.values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"'{tab}'!A1:AC2000"
        ).execute().get("values", [])
        if not live[tab]:
            raise RuntimeError(f"{tab} tab is empty")

    payload = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    li_header, *li_values = live["LinkedIn"]
    ig_header, *ig_values = live["Instagram"]
    existing_li = [dict(zip(li_header, row)) for row in li_values]
    existing_ig = [dict(zip(ig_header, row)) for row in ig_values]

    li_urls = {key(row.get("Person LinkedIn URL")) for row in existing_li}
    cohort_li_urls = {key(row.get("Person LinkedIn URL")) for row in payload["linkedin_rows"]}
    missing_linkedin = cohort_li_urls - li_urls
    if missing_linkedin:
        raise RuntimeError(
            f"LinkedIn tab is missing {len(missing_linkedin)} cohort people; refusing a partial alignment"
        )

    existing_people = {name_company(row) for row in existing_ig}
    existing_ig_urls = {key(row.get("Person Instagram URL")) for row in existing_ig}
    to_append: list[dict] = []
    skipped_existing = 0
    for row in payload["instagram_rows"]:
        if name_company(row) in existing_people:
            skipped_existing += 1
            continue
        ig_url = key(row.get("Person Instagram URL"))
        if not ig_url or ig_url in existing_ig_urls:
            raise RuntimeError(f"Missing or conflicting Instagram URL for {row.get('Person Name')}")
        to_append.append(row)
        existing_people.add(name_company(row))
        existing_ig_urls.add(ig_url)

    value_rows = []
    for row in to_append:
        enriched = dict(row)
        status = row.get("Instagram Profile Status", "")
        verification = row.get("Verification Status", "")
        if verification == "FINAL PASS":
            scrape_status = "SOP FINAL"
        elif status == "PROFILE EXISTS — REVIEW":
            scrape_status = "PROFILE EXISTS - REVIEW"
        else:
            scrape_status = "DATABASE CANDIDATE - REVIEW"
        enriched["Scrape Status"] = scrape_status
        enriched["Last Scraped"] = (
            date(2026, 9, 7).isoformat()
            if status in {"VERIFIED", "PROFILE EXISTS — REVIEW"}
            else ""
        )
        value_rows.append([enriched.get(column, "") for column in ig_header])

    result = {
        "spreadsheet_title": meta["properties"]["title"],
        "linkedin_cohort_present": len(cohort_li_urls),
        "instagram_cohort_already_present": skipped_existing,
        "instagram_rows_to_append": len(value_rows),
        "applied": False,
    }
    if args.apply and value_rows:
        previous_last_row = len(live["Instagram"])
        response = sheets_api.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="'Instagram'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": value_rows},
        ).execute()
        updated_range = response["updates"]["updatedRange"]
        start_row, end_row = a1_row_range(updated_range)
        sheet_id = properties["Instagram"]["sheetId"]
        sheets_api.batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [
                {
                    "copyPaste": {
                        "source": {
                            "sheetId": sheet_id,
                            "startRowIndex": previous_last_row - 1,
                            "endRowIndex": previous_last_row,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(ig_header),
                        },
                        "destination": {
                            "sheetId": sheet_id,
                            "startRowIndex": start_row - 1,
                            "endRowIndex": end_row,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(ig_header),
                        },
                        "pasteType": "PASTE_FORMAT",
                        "pasteOrientation": "NORMAL",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": start_row - 1,
                            "endRowIndex": end_row,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(ig_header),
                        },
                        "cell": {"userEnteredFormat": {"verticalAlignment": "TOP", "wrapStrategy": "WRAP"}},
                        "fields": "userEnteredFormat.verticalAlignment,userEnteredFormat.wrapStrategy",
                    }
                },
            ]},
        ).execute()
        result.update({
            "applied": True,
            "updated_range": updated_range,
            "appended": len(value_rows),
        })

    AUDIT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
