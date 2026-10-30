"""Append LinkedIn FINAL PASS rows found since the 2026-10-12 baseline to Google Sheets.

Run after each loop cycle. Append-only: rows already in the sheet (by personal
LinkedIn URL or company+person) are skipped, as is anyone who was FINAL PASS before
this collection run started (data/reference/linkedin_final_baseline_20261012.txt).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config, used_contacts  # noqa: E402
from mp.normalize import is_distinctive_company_name  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.sop import scoring_v4  # noqa: E402
from mp.stages.s8_score import build_facts  # noqa: E402
from mp.stages.s9_export import _mandatory_ok, _row_for  # noqa: E402

SPREADSHEET_ID = "1OihYVSE8SGM2nKiBqyXK4Aps1jz6NrKcfTpdrgmyRJc"
CREDENTIALS = "/Users/anirudhtyagi/Downloads/rosy-spring-507811-c2-79a06cc1bdba.json"
BASELINE = ROOT / "data/reference/linkedin_final_baseline_20261012.txt"
AUDIT = ROOT / "data/google_sheet_sync_linkedin_cycles_20261012.jsonl"
BATCH = "linkedin_cycles_20261012"
# --- SOP 01/02 v4.0 (2026-10-13) gates applied to NEW rows --------------------
# Person SOP SS3 recommends one PRIMARY + two ALTERNATE, but labels it an INTERNAL
# quota. Relaxed 2026-10-15 at the user's request to recover yield: up to 6 per
# company (the old SOP's mid-band target), still strongest-score first.
COMPANY_CAP = 6
# Company SOP SS7: Platform subtypes. Projects lanes (LIVE 6+ events, CATALOG, D2C)
# need lane proof this pipeline does not collect, so those companies are held.
PLATFORM_SUBTYPE = {"record_label": "Record Label",
                    "artist_management": "Artist Management",
                    "music_distributor": "Boutique Distributor",
                    "label_services": "Boutique Distributor"}
# Company SOP SS7: 3-25 is a PREFERENCE, ">25 not automatically reject". The A2
# roster score already penalises poor fit, so only single-artist or unknown rosters
# are held (relaxed 2026-10-15 at the user's request).
ROSTER_MIN = 2
LIVE_LABEL = "Promoter / Live (6+ Events)"
# Which lanes this run appends (config/sources.yaml sheet.lanes). The 2026-10-25
# run is promoters only; labels/management/distributors are paused.
LANES = set((config.sources().get("sheet") or {}).get("lanes", ["PLATFORM", "LIVE"]))
# No CRM / relationship register is reachable from here and no QA reviewer signs
# off in this script, so a row can only ever be research-verified, pending QA
# (Company SOP SS10, Person SOP SS8/SS11). Never label it FINAL or CLEAR.
STATUS_LABEL = "VERIFIED_CURRENT - PENDING_QA (relationship not cleared)"


def sop_v4_hold(ctx, person_id: int, score_row, lane: str = "PLATFORM") -> str:
    """Reason this person cannot be a new-SOP research row, or '' when eligible."""
    info = ctx.db.one("""SELECT c.company_type, c.roster_estimate, c.country,
                                p.title_family, p.seniority
                         FROM people p JOIN companies c USING(company_id)
                         WHERE p.person_id=?""", (person_id,))
    if lane not in LANES:
        return f"lane not in this run ({lane})"
    # Client instruction 2026-10-25: no Indian companies or people.
    if (info["country"] or "").upper() == "IN" or ctx.db.one(
            """SELECT 1 FROM person_evidence WHERE person_id=? AND kind='location'
               AND lower(snippet) LIKE '%india%'""", (person_id,)):
        return "excluded market (India)"
    if lane == "PLATFORM":
        if info["company_type"] not in PLATFORM_SUBTYPE:
            return f"company lane unproven ({info['company_type'] or 'unknown type'})"
        roster = info["roster_estimate"]
        if roster is None or roster < ROSTER_MIN:
            return f"ROSTER_UNVERIFIED/single-artist ({roster})"
    # Person-fit floors (C >= 20, current employer/title >= 6) are applied inside
    # scoring_v4's FINAL PASS verdict.
    # Person SOP SS6.2 / SS11: same-name employer collisions are the main source of
    # false rows. The headline must clearly be about THIS music company.
    from mp.sources import groq
    company = ctx.db.one("""SELECT c.canonical_name AS name, c.domain, c.company_type AS type,
                                   c.country, p.full_name, p.title
                            FROM people p JOIN companies c USING(company_id)
                            WHERE p.person_id=?""", (person_id,))
    # Found through the company's own LinkedIn page (current position linked to that
    # page): employer identity is already proven, no name-based check needed.
    page_match = ctx.db.one("""SELECT 1 FROM person_evidence WHERE person_id=?
                                AND source='apify_linkedin_company_page'""", (person_id,))
    verdict, why = groq.employer_match(ctx.http, dict(company), company["full_name"],
                                       company["title"] or "")
    # A company-page match only needs to avoid a clear NO (LinkedIn occasionally links
    # an unrelated role to a label page, e.g. an architect on Decca's page).
    if page_match and verdict != "NO":
        verdict = "YES"
    # A company whose name has already produced several confirmed collisions
    # ("Kompakt" -> design studios) only accepts people found via its company page.
    collisions = ctx.db.one("""SELECT COUNT(*) n FROM people p JOIN people q USING(company_id)
                               WHERE q.person_id=? AND p.status='REJECTED'
                                 AND (p.reject_reason LIKE 'Manual review%'
                                      OR p.reject_reason LIKE 'Wrong employer%')""",
                            (person_id,))["n"]
    if collisions >= 3 and not page_match:
        return f"EMPLOYER_CONFLICT (collision-prone company: {collisions} prior mismatches)"
    # UNCLEAR usually means a bare headline ("Owner"); acceptable only when the
    # company name is distinctive enough that a same-name employer is unlikely.
    # Name-search people need a clear YES: an UNCLEAR "Owner" at a distinctive-sounding
    # name ("Dharma Records", "Oh Boy Records") still turned out to be a stranger
    # about half the time.
    if verdict != "YES":
        return f"EMPLOYER_CONFLICT ({verdict or 'unchecked'}: {why[:80]})"
    # SS4.5: A&R only when the remit is cross-artist; accept head-level titles only.
    if info["title_family"] == "anr_artist_development" and info["seniority"] not in ("executive", "senior"):
        return "ROLE_NOT_RELEVANT (A&R below head level)"
    return ""


def key(value: object) -> str:
    return str(value or "").strip().lower().rstrip("/")


def row_numbers(updated_range: str) -> tuple[int, int]:
    match = re.search(r"![A-Z]+(\d+):[A-Z]+(\d+)$", updated_range)
    if not match:
        raise RuntimeError(f"Unexpected updated range: {updated_range}")
    return int(match.group(1)), int(match.group(2))


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    sop = config.sop()
    baseline = {int(line) for line in BASELINE.read_text().split()}
    ctx = Context.create(db_path="data/master.sqlite")
    candidates: list[tuple[int, dict]] = []
    skipped: list[dict] = []
    try:
        # Score with Person SOP v4 SS9.2 (the governing standard since 2026-10-13):
        # same 90 floor and hard gates, but no points for roster size and current
        # activity scored on being documented, not on 90-day release volume.
        pool = ctx.db.q("""SELECT s.* FROM scores s JOIN people p USING(person_id)
                           JOIN companies c USING(company_id)
                           WHERE s.channel='linkedin' AND p.status='VERIFIED'
                             AND c.status='QUALIFIED'""")
        scored = []
        for score_row in pool:
            if score_row["person_id"] in baseline:
                continue
            facts = build_facts(ctx, score_row["person_id"])
            v4 = scoring_v4.score(facts, "linkedin")
            if v4.verdict == "FINAL PASS":
                scored.append((v4, facts, {**dict(score_row), "total": v4.total,
                                           "subscores": json.dumps(v4.subscores)}))
        scored.sort(key=lambda item: -item[0].total)        # strongest first = PRIMARY
        for v4, facts, score_row in scored:
            person_id = score_row["person_id"]
            row = _row_for(facts, score_row, "linkedin", sop)
            ok, missing = _mandatory_ok(
                row, sop["sheets"]["linkedin"]["mandatory"], sop["sheets"]["not_found_literal"]
            )
            if not ok:
                skipped.append({"person_id": person_id, "reason": f"missing mandatory: {missing}"})
                continue
            if not key(row.get("Person LinkedIn URL")).startswith("https://www.linkedin.com/in/"):
                skipped.append({"person_id": person_id, "reason": "non-canonical LinkedIn URL"})
                continue
            if (used_contacts.company_blocked(row.get("Company Name"))
                    or used_contacts.person_blocked(row.get("Person Name"),
                                                    row.get("Person LinkedIn URL"))):
                skipped.append({"person_id": person_id, "reason": "used contact/company (exclusion list)"})
                continue
            hold = sop_v4_hold(ctx, person_id, score_row, facts.get("lane", "PLATFORM"))
            if hold:
                skipped.append({"person_id": person_id, "reason": hold})
                continue
            if facts.get("lane") == "LIVE":
                row["Company Type"] = LIVE_LABEL
                past, nxt = facts.get("live_events_past", 0), facts.get("live_events_next", 0)
                row["Live Event Window"] = (
                    "Past and next 12 months" if past >= 6 and nxt >= 6
                    else "Past 12 months" if past >= 6 else "Next 12 months")
                row["Promoted Events Counted"] = (
                    f"{past} in past 12 months | {nxt} upcoming in next 12 months "
                    f"(promoter of record; event proof stored)")
            else:
                row["Company Type"] = PLATFORM_SUBTYPE[ctx.db.one(
                    """SELECT c.company_type FROM people p JOIN companies c USING(company_id)
                       WHERE p.person_id=?""", (person_id,))["company_type"]]
            row["Verification Status"] = STATUS_LABEL
            candidates.append((person_id, row))
    finally:
        ctx.close()

    creds = Credentials.from_service_account_file(
        CREDENTIALS, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    api = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
    meta = api.get(spreadsheetId=SPREADSHEET_ID, fields="sheets.properties").execute()
    props = next(s["properties"] for s in meta["sheets"] if s["properties"]["title"] == "LinkedIn")
    live = api.values().get(
        spreadsheetId=SPREADSHEET_ID, range="'LinkedIn'!A1:AC10000"
    ).execute().get("values", [])
    if not live:
        raise RuntimeError("LinkedIn tab is empty")
    header, *values = live
    # The tab also holds a company-list block mid-sheet with its own header; only
    # rows carrying a personal LinkedIn URL are people.
    existing = [row for row in (dict(zip(header, r)) for r in values)
                if "linkedin.com/in/" in key(row.get("Person LinkedIn URL"))]
    existing_urls = {key(row.get("Person LinkedIn URL")) for row in existing}
    existing_people = {(key(row.get("Company Name")), key(row.get("Person Name"))) for row in existing}
    company_counts = Counter(key(row.get("Company Name")) for row in existing)

    append_rows: list[list] = []
    appended_ids: list[int] = []
    for person_id, row in candidates:
        url = key(row.get("Person LinkedIn URL"))
        person = (key(row.get("Company Name")), key(row.get("Person Name")))
        company = key(row.get("Company Name"))
        if url in existing_urls or person in existing_people:
            skipped.append({"person_id": person_id, "reason": "already present"})
            continue
        if company_counts[company] >= COMPANY_CAP:
            skipped.append({"person_id": person_id, "reason": "company quota (1 primary + 2 alternates)"})
            continue
        enriched = dict(row)
        enriched["Scrape Status"] = "SOP FINAL"
        append_rows.append([enriched.get(column, "") for column in header])
        appended_ids.append(person_id)
        existing_urls.add(url)
        existing_people.add(person)
        company_counts[company] += 1

    result = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "dry_run": dry_run, "rows_before": len(values),
              "final_candidates": len(candidates), "skipped": skipped,
              "appended": 0 if dry_run else len(append_rows),
              "would_append": len(append_rows), "appended_ids": appended_ids}
    if append_rows and not dry_run:
        # Write below the last used row. values.append() targets the first "table"
        # it detects from A1, which ends at the blank row inside the company block,
        # so it inserted people mid-sheet instead of at the bottom.
        first = len(live) + 1
        grid_rows = props["gridProperties"]["rowCount"]
        if first + len(append_rows) - 1 > grid_rows:
            api.batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [{"appendDimension": {
                "sheetId": props["sheetId"], "dimension": "ROWS",
                "length": first + len(append_rows) - 1 - grid_rows}}]}).execute()
        response = api.values().update(
            spreadsheetId=SPREADSHEET_ID, range=f"'LinkedIn'!A{first}",
            valueInputOption="USER_ENTERED", body={"values": append_rows},
        ).execute()
        updated_range = response["updatedRange"]
        first_row, last_row = row_numbers(updated_range)
        # New rows stay WHITE: never copy the colour of the row above (client request).
        api.batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [{"repeatCell": {
            "range": {"sheetId": props["sheetId"], "startRowIndex": first_row - 1,
                      "endRowIndex": last_row, "startColumnIndex": 0, "endColumnIndex": len(header)},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}},
            "fields": "userEnteredFormat.backgroundColor",
        }}]}).execute()
        con = sqlite3.connect(ROOT / "data/master.sqlite", timeout=60)
        try:
            for person_id in appended_ids:
                con.execute(
                    "INSERT OR IGNORE INTO sheet_membership(person_id,channel,first_seen_at,batch) VALUES (?, 'linkedin', datetime('now'), ?)",
                    (person_id, BATCH),
                )
            con.commit()
        finally:
            con.close()
        verify = api.values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"'LinkedIn'!A{first_row}:AC{last_row}"
        ).execute().get("values", [])
        if len(verify) != len(append_rows):
            raise RuntimeError("Post-append row-count verification failed")
        result.update({"updated_range": updated_range, "verified_rows": len(verify)})
    if not dry_run:
        with AUDIT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result) + "\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
