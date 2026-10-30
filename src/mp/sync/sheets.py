"""Google Sheets append-sync for the two final sheets.

Local CSV/XLSX remain the system of record; this pushes new rows into two named
tabs. The sync is APPEND-ONLY and de-duplicates against what is already in the
sheet using the SS52 person identifiers, so re-running never creates duplicate rows.

Requires `google-api-python-client` + (`google-auth` for a service account, or
`google-auth-oauthlib` for interactive OAuth) and a credentials file. Both auth
paths are optional: without them the pipeline is unaffected.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .. import config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TABS = {"linkedin": "LinkedIn", "instagram": "Instagram"}


def _client(credentials_path: str, token_path: str):
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Google Sheets sync needs extra packages. Install with:\n"
            "  pip install google-api-python-client google-auth") from exc

    creds_data = json.loads(Path(credentials_path).read_text())
    if creds_data.get("type") == "service_account":
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
    else:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = None
        token = Path(token_path)
        if token.exists():
            creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
                creds = flow.run_local_server(port=0)
            token.write_text(creds.to_json())
    return build("sheets", "v4", credentials=creds)


ID_COLUMN = {"linkedin": "Person LinkedIn URL", "instagram": "Person Instagram URL"}


def _identity(row: dict, channel: str) -> str:
    """SS52 identity for dedupe against rows already in the sheet."""
    primary = row.get(ID_COLUMN[channel], "")
    if primary and primary != "Not Found":
        return primary.strip().lower().rstrip("/")
    return f"{row.get('Person Name','').strip().lower()}|{row.get('Company Name','').strip().lower()}"


def _has_mandatory_id(row: dict, channel: str) -> bool:
    """Never push a row missing its channel's own person identifier."""
    v = (row.get(ID_COLUMN[channel]) or "").strip()
    return bool(v) and v != "Not Found"


def sync(run_dir: str | Path, spreadsheet_id: str, *,
         credentials_path: str = "credentials.json",
         token_path: str = "token.json", dry_run: bool = False) -> dict:
    run_dir = Path(run_dir)
    sop = config.sop()
    results: dict[str, dict] = {}

    service = None if dry_run else _client(credentials_path, token_path)

    for channel, tab in TABS.items():
        csv_path = run_dir / f"{channel}_final.csv"
        if not csv_path.exists():
            results[channel] = {"error": f"missing {csv_path}"}
            continue

        with csv_path.open(newline="", encoding="utf-8") as fh:
            local_rows = list(csv.DictReader(fh))
        sop_columns = sop["sheets"][channel]["columns"]

        # Missing/blank mandatory ID (SS53: never fabricate) never leaves the machine.
        local_rows = [r for r in local_rows if _has_mandatory_id(r, channel)]

        if dry_run:
            results[channel] = {"would_append": len(local_rows), "tab": tab}
            continue

        sheet = service.spreadsheets()
        existing = sheet.values().get(spreadsheetId=spreadsheet_id,
                                      range=f"'{tab}'!A:Z").execute().get("values", [])

        if not existing:
            sheet.values().update(
                spreadsheetId=spreadsheet_id, range=f"'{tab}'!A1",
                valueInputOption="RAW", body={"values": [sop_columns]}).execute()
            existing = [sop_columns]

        # Follow the sheet's OWN existing header order/columns, not the SOP's
        # internal order -- a hand-maintained tracking sheet may have a
        # different column layout than the pipeline's own final_prospects.xlsx.
        header = existing[0]
        seen = {_identity(dict(zip(header, r)), channel) for r in existing[1:]}

        new_rows = [[r.get(c, "") for c in header] for r in local_rows
                    if _identity(r, channel) not in seen]

        if new_rows:
            sheet.values().append(
                spreadsheetId=spreadsheet_id, range=f"'{tab}'!A1",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                body={"values": new_rows}).execute()

        results[channel] = {"appended": len(new_rows),
                            "skipped_duplicates": len(local_rows) - len(new_rows),
                            "tab": tab}
    return results
