"""Append strongly bound Instagram review candidates, clearly marked non-final."""
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

SHEET_ID = "1OihYVSE8SGM2nKiBqyXK4Aps1jz6NrKcfTpdrgmyRJc"
CREDENTIALS = "/Users/anirudhtyagi/Downloads/rosy-spring-507811-c2-79a06cc1bdba.json"
AUDIT = ROOT / "data/google_sheet_sync_instagram_review_20260911.json"
EXCLUDED = {134, 363, 381, 569, 1700, 2035, 2344, 2348, 2445, 2457}


def key(value: object) -> str:
    return str(value or "").strip().lower().rstrip("/")


def main() -> int:
    con = sqlite3.connect(ROOT / "data/master.sqlite")
    ids = [int(row[0]) for row in con.execute(
        """SELECT DISTINCT p.person_id
             FROM people p JOIN profiles pr USING(person_id)
             JOIN scores s ON s.person_id=p.person_id AND s.channel='instagram'
            WHERE pr.channel='instagram' AND pr.verification_status='VERIFIED'
              AND s.verdict!='FINAL PASS' AND s.total>=75
              AND json_extract(pr.match_signals,'$.matches_linkedin_identity')=1
              AND (json_extract(pr.match_signals,'$.company_in_bio')=1
                OR json_extract(pr.match_signals,'$.company_tagged')=1
                OR json_extract(pr.match_signals,'$.external_links_confirm')=1
                OR json_extract(pr.match_signals,'$.company_account_follows')=1)
            ORDER BY s.total DESC,p.person_id"""
    ) if int(row[0]) not in EXCLUDED]
    con.close()
    sop = config.sop()
    ctx = Context.create(db_path="data/master.sqlite")
    rows = []
    try:
        for pid in ids:
            score = ctx.db.one("SELECT * FROM scores WHERE person_id=? AND channel='instagram'", (pid,))
            facts = build_facts(ctx, pid)
            row = _row_for(facts, score, "instagram", sop)
            ok, missing = _mandatory_ok(row, sop["sheets"]["instagram"]["mandatory"], sop["sheets"]["not_found_literal"])
            if ok:
                rows.append((pid, row, score["total"], score["verdict"]))
    finally:
        ctx.close()

    creds = Credentials.from_service_account_file(CREDENTIALS, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    api = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
    meta = api.get(spreadsheetId=SHEET_ID, fields="sheets.properties").execute()
    props = next(s["properties"] for s in meta["sheets"] if s["properties"]["title"] == "Instagram")
    live = api.values().get(spreadsheetId=SHEET_ID, range="'Instagram'!A1:AC2000").execute().get("values", [])
    header, *values = live
    existing = [dict(zip(header, row)) for row in values]
    urls = {key(row.get("Person Instagram URL")) for row in existing}
    people = {(key(row.get("Company Name")), key(row.get("Person Name"))) for row in existing}
    counts = Counter(key(row.get("Company Name")) for row in existing)
    output, added = [], []
    for pid, row, total, verdict in rows:
        company = key(row.get("Company Name")); person = (company, key(row.get("Person Name"))); url = key(row.get("Person Instagram URL"))
        if url in urls or person in people or counts[company] >= 10:
            continue
        enriched = dict(row)
        enriched["Scrape Status"] = f"SOP REVIEW – NOT FINAL ({total})"
        enriched["Last Scraped"] = "2026-09-11"
        output.append([enriched.get(col, "") for col in header])
        added.append(pid); urls.add(url); people.add(person); counts[company] += 1
    result = {"rows_before": len(values), "eligible": len(rows), "appended": len(output), "person_ids": added}
    if output:
        response = api.values().append(spreadsheetId=SHEET_ID, range="'Instagram'!A1", valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS", body={"values": output}).execute()
        updated = response["updates"]["updatedRange"]
        nums = [int(x) for x in re.findall(r"\d+", updated)]
        start, end = nums[-2], nums[-1]
        api.batchUpdate(spreadsheetId=SHEET_ID, body={"requests": [{"copyPaste": {"source": {"sheetId": props["sheetId"], "startRowIndex": start-2, "endRowIndex": start-1, "startColumnIndex": 0, "endColumnIndex": len(header)}, "destination": {"sheetId": props["sheetId"], "startRowIndex": start-1, "endRowIndex": end, "startColumnIndex": 0, "endColumnIndex": len(header)}, "pasteType": "PASTE_FORMAT", "pasteOrientation": "NORMAL"}}]}).execute()
        result["updated_range"] = updated
    AUDIT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
