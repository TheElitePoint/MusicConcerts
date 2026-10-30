"""Append newly verified 2026-09-15 LinkedIn FINAL PASS rows to Google Sheets."""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.stages.s8_score import build_facts  # noqa: E402
from mp.stages.s9_export import _mandatory_ok, _row_for  # noqa: E402

SPREADSHEET_ID = "1OihYVSE8SGM2nKiBqyXK4Aps1jz6NrKcfTpdrgmyRJc"
CREDENTIALS = "/Users/anirudhtyagi/Downloads/rosy-spring-507811-c2-79a06cc1bdba.json"
AUDIT = ROOT / "data/google_sheet_sync_linkedin_new_run_20260915.json"
PERSON_IDS = [1683, 2960, 2962]


def key(value: object) -> str:
    return str(value or "").strip().lower().rstrip("/")


def row_numbers(updated_range: str) -> tuple[int, int]:
    match = re.search(r"![A-Z]+(\d+):[A-Z]+(\d+)$", updated_range)
    if not match:
        raise RuntimeError(f"Unexpected updated range: {updated_range}")
    return int(match.group(1)), int(match.group(2))


def main() -> int:
    sop = config.sop()
    ctx = Context.create(db_path="data/master.sqlite")
    candidates: list[tuple[int, dict]] = []
    try:
        for person_id in PERSON_IDS:
            score_row = ctx.db.one(
                "SELECT * FROM scores WHERE person_id=? AND channel='linkedin'",
                (person_id,),
            )
            if not score_row or score_row["verdict"] != "FINAL PASS" or int(score_row["total"]) < 90:
                continue
            facts = build_facts(ctx, person_id)
            row = _row_for(facts, score_row, "linkedin", sop)
            ok, missing = _mandatory_ok(
                row, sop["sheets"]["linkedin"]["mandatory"], sop["sheets"]["not_found_literal"]
            )
            if not ok:
                raise RuntimeError(f"person_id={person_id} missing mandatory fields: {missing}")
            candidates.append((person_id, row))
    finally:
        ctx.close()

    creds = Credentials.from_service_account_file(
        CREDENTIALS, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    api = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
    meta = api.get(spreadsheetId=SPREADSHEET_ID, fields="properties.title,sheets.properties").execute()
    props = next(s["properties"] for s in meta["sheets"] if s["properties"]["title"] == "LinkedIn")
    live = api.values().get(
        spreadsheetId=SPREADSHEET_ID, range="'LinkedIn'!A1:AC3000"
    ).execute().get("values", [])
    if not live:
        raise RuntimeError("LinkedIn tab is empty")
    header, *values = live
    existing = [dict(zip(header, row)) for row in values]
    existing_urls = {key(row.get("Person LinkedIn URL")) for row in existing}
    existing_people = {(key(row.get("Company Name")), key(row.get("Person Name"))) for row in existing}
    company_counts = Counter(key(row.get("Company Name")) for row in existing)

    append_rows: list[list] = []
    appended_ids: list[int] = []
    skipped: list[dict] = []
    for person_id, row in candidates:
        url = key(row.get("Person LinkedIn URL"))
        person = (key(row.get("Company Name")), key(row.get("Person Name")))
        company = key(row.get("Company Name"))
        if not url.startswith("https://www.linkedin.com/in/"):
            raise RuntimeError(f"Non-canonical personal LinkedIn URL for person_id={person_id}")
        if url in existing_urls or person in existing_people:
            skipped.append({"person_id": person_id, "reason": "already present"})
            continue
        if company_counts[company] >= 10:
            skipped.append({"person_id": person_id, "reason": "company cap"})
            continue
        enriched = dict(row)
        enriched["Scrape Status"] = "SOP FINAL"
        append_rows.append([enriched.get(column, "") for column in header])
        appended_ids.append(person_id)
        existing_urls.add(url)
        existing_people.add(person)
        company_counts[company] += 1

    result = {"rows_before": len(values), "final_candidates": len(candidates), "skipped": skipped}
    if append_rows:
        response = api.values().append(
            spreadsheetId=SPREADSHEET_ID, range="'LinkedIn'!A1",
            valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
            body={"values": append_rows},
        ).execute()
        updated_range = response["updates"]["updatedRange"]
        first_row, last_row = row_numbers(updated_range)
        api.batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [{"copyPaste": {
            "source": {"sheetId": props["sheetId"], "startRowIndex": first_row - 2,
                       "endRowIndex": first_row - 1, "startColumnIndex": 0, "endColumnIndex": len(header)},
            "destination": {"sheetId": props["sheetId"], "startRowIndex": first_row - 1,
                            "endRowIndex": last_row, "startColumnIndex": 0, "endColumnIndex": len(header)},
            "pasteType": "PASTE_FORMAT", "pasteOrientation": "NORMAL",
        }}]}).execute()
        con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
        try:
            for person_id in appended_ids:
                con.execute(
                    "INSERT OR IGNORE INTO sheet_membership(person_id,channel,first_seen_at,batch) VALUES (?, 'linkedin', datetime('now'), ?)",
                    (person_id, "linkedin_new_labels_20260915"),
                )
            con.commit()
        finally:
            con.close()
        verify = api.values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"'LinkedIn'!A{first_row}:AC{last_row}"
        ).execute().get("values", [])
        if len(verify) != len(append_rows):
            raise RuntimeError("Post-append row-count verification failed")
        result.update({"appended": len(append_rows), "appended_ids": appended_ids,
                       "updated_range": updated_range, "verified_rows": len(verify)})
    else:
        result["appended"] = 0
    AUDIT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
