"""Rebuild the main Instagram tab from unique SOP FINAL records only."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parents[1]
SPREADSHEET_ID = "1OihYVSE8SGM2nKiBqyXK4Aps1jz6NrKcfTpdrgmyRJc"
CREDENTIALS = "/Users/anirudhtyagi/Downloads/rosy-spring-507811-c2-79a06cc1bdba.json"
AUDIT_TAB = "Instagram Candidate Audit"
BACKUP = ROOT / "data/google_instagram_full_tab_backup_before_strict_audit_20260907.json"
RESULT = ROOT / "data/google_instagram_full_correction_20260907.json"


def key(value: str | None) -> str:
    return (value or "").strip().lower().rstrip("/")


def main() -> int:
    creds = Credentials.from_service_account_file(
        CREDENTIALS, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    api = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
    meta = api.get(
        spreadsheetId=SPREADSHEET_ID, fields="properties.title,sheets.properties"
    ).execute()
    props = {s["properties"]["title"]: s["properties"] for s in meta["sheets"]}
    if "Instagram" not in props or AUDIT_TAB not in props:
        raise RuntimeError("Required Instagram or audit tab is missing")

    main_values = api.values().get(
        spreadsheetId=SPREADSHEET_ID, range="'Instagram'!A1:AC400"
    ).execute().get("values", [])
    audit_values = api.values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{AUDIT_TAB}'!A1:AL500"
    ).execute().get("values", [])
    if len(main_values) != 106 or len(audit_values) != 161:
        raise RuntimeError(f"Unexpected live sizes: Instagram={len(main_values)}, audit={len(audit_values)}")
    header = main_values[0]
    audit_header = audit_values[0]
    rows = [dict(zip(header, row)) for row in main_values[1:] if any(str(v).strip() for v in row)]

    con = sqlite3.connect(ROOT / "data/master.sqlite")
    con.row_factory = sqlite3.Row
    evidence = {
        key(r["linkedin_url"]): dict(r)
        for r in con.execute(
            """SELECT li.url AS linkedin_url,ig.url AS instagram_url,
                      ig.verification_status,ig.identity_score,ig.match_signals,
                      si.total AS score,si.verdict
                 FROM profiles li
                 LEFT JOIN profiles ig ON ig.person_id=li.person_id AND ig.channel='instagram'
                 LEFT JOIN scores si ON si.person_id=li.person_id AND si.channel='instagram'
                WHERE li.channel='linkedin'"""
        )
    }
    con.close()

    keep: list[dict] = []
    move: list[tuple[dict, str, str, dict]] = []
    dropped_placeholders = 0
    seen_people: set[tuple[str, str]] = set()
    seen_handles: set[str] = set()
    for row in rows:
        if not row.get("Person Name") and not row.get("Person Instagram URL"):
            dropped_placeholders += 1
            continue
        ev = evidence.get(key(row.get("Person LinkedIn URL")), {})
        same_profile = key(ev.get("instagram_url")) == key(row.get("Person Instagram URL"))
        sop_final = (
            same_profile
            and ev.get("verification_status") == "VERIFIED"
            and ev.get("verdict") == "FINAL PASS"
            and int(ev.get("score") or 0) >= 90
        )
        person_key = (key(row.get("Person Name")), key(row.get("Company Name")))
        handle_key = key(row.get("Person Instagram URL"))
        duplicate = person_key in seen_people or handle_key in seen_handles
        if sop_final and not duplicate:
            keep.append(row)
            seen_people.add(person_key)
            seen_handles.add(handle_key)
            continue
        if duplicate:
            decision = "DUPLICATE"
            reason = "Exact person/profile duplicate already retained in the main Instagram FINAL tab."
        elif same_profile and ev.get("verification_status") == "VERIFIED":
            decision = "RESEARCH FURTHER"
            reason = f"Profile identity is stored as verified, but the Instagram score/verdict ({ev.get('score')}, {ev.get('verdict')}) does not meet FINAL rules."
        elif row.get("Verification Status") != "FINAL PASS":
            decision = "RESEARCH FURTHER"
            reason = "The source row was already below FINAL status and does not belong in the main FINAL tab."
        else:
            decision = "UNRESOLVED"
            reason = "The profile does not match a verified SOP Instagram identity record for this LinkedIn person."
        move.append((row, decision, reason, ev))

    if len(keep) != 59 or len(move) != 45 or dropped_placeholders != 1:
        raise RuntimeError(
            f"Expected 59 unique FINAL, 45 audit moves, and 1 placeholder; "
            f"found {len(keep)}, {len(move)}, and {dropped_placeholders}"
        )
    BACKUP.write_text(json.dumps({"header": header, "rows": main_values[1:]}, ensure_ascii=False, indent=2), encoding="utf-8")

    appended_audit = []
    for row, decision, reason, ev in move:
        extra = {
            "Identity Audit Decision": decision,
            "Identity Audit Reason": reason,
            "Audited IG Display Name": row.get("IG Full Name", ""),
            "Audited IG Bio": row.get("IG Bio", ""),
            "Name Exact": "",
            "Name Similarity": "",
            "Company Signal": "",
            "Company Domain Signal": "",
            "Stored Verification": ev.get("verification_status") or "",
        }
        appended_audit.append([row.get(column, extra.get(column, "")) for column in audit_header])

    # Preserve the main sheet structure and formatting. Replace contents only.
    api.values().clear(
        spreadsheetId=SPREADSHEET_ID, range="'Instagram'!A2:AC400", body={}
    ).execute()
    api.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range="'Instagram'!A2",
        valueInputOption="USER_ENTERED",
        body={"values": [[row.get(column, "") for column in header] for row in keep]},
    ).execute()
    api.values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{AUDIT_TAB}'!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": appended_audit},
    ).execute()

    # Apply the existing audit body style to the new audit rows and keep main rows wrapped.
    main_id = props["Instagram"]["sheetId"]
    audit_id = props[AUDIT_TAB]["sheetId"]
    api.batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [
            {
                "copyPaste": {
                    "source": {"sheetId": audit_id, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": len(audit_header)},
                    "destination": {"sheetId": audit_id, "startRowIndex": 161, "endRowIndex": 206, "startColumnIndex": 0, "endColumnIndex": len(audit_header)},
                    "pasteType": "PASTE_FORMAT", "pasteOrientation": "NORMAL",
                }
            },
            {
                "repeatCell": {
                    "range": {"sheetId": main_id, "startRowIndex": 1, "endRowIndex": 60, "startColumnIndex": 0, "endColumnIndex": len(header)},
                    "cell": {"userEnteredFormat": {"verticalAlignment": "TOP", "wrapStrategy": "WRAP"}},
                    "fields": "userEnteredFormat.verticalAlignment,userEnteredFormat.wrapStrategy",
                }
            },
            {"clearBasicFilter": {"sheetId": audit_id}},
            {"setBasicFilter": {"filter": {"range": {"sheetId": audit_id, "startRowIndex": 0, "endRowIndex": 206, "startColumnIndex": 0, "endColumnIndex": len(audit_header)}}}},
        ]},
    ).execute()

    counts = {}
    for _, decision, _, _ in move:
        counts[decision] = counts.get(decision, 0) + 1
    result = {
        "spreadsheet": meta["properties"]["title"],
        "instagram_unique_sop_final": len(keep),
        "moved_to_audit": len(move),
        "moved_breakdown": counts,
        "dropped_blank_placeholders": dropped_placeholders,
        "audit_total_candidates": 160 + len(move),
        "backup": str(BACKUP),
    }
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
