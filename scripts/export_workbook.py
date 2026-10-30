"""Build a complete Excel workbook of everything researched.

The SOP's two final sheets stay exactly as specified (SS47/SS48) and contain only
FINAL PASS records. The additional tabs are working material -- qualified companies,
near-miss people, the review queue and rejections -- kept strictly separate so the
final sheets are never diluted (SS1: "Keep intermediate/review/rejected data
separate from final sheets").
"""
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openpyxl import Workbook                                     # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill          # noqa: E402
from openpyxl.utils import get_column_letter                      # noqa: E402

from mp import config                                             # noqa: E402
from mp.db import Database, utcnow                                # noqa: E402

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True)
NF = "Not Found"


def add_sheet(wb, title, columns, rows, widths=None):
    ws = wb.create_sheet(title[:31])
    ws.append(columns)
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in rows:
        ws.append([row.get(c, "") for c in columns])
    for i, col in enumerate(columns, start=1):
        if widths and col in widths:
            width = widths[col]
        elif col.startswith("Why") or col.endswith("Evidence") or "Reason" in col:
            width = 52
        elif "URL" in col or "Website" in col:
            width = 38
        else:
            width = 22
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return ws


def record_membership(db, channel: str, rows: list[dict], batch: str) -> set[int]:
    """Record first appearance in a sheet; return the ids that are NEW this batch."""
    new_ids: set[int] = set()
    for row in rows:
        pid = row.get("_person_id")
        if pid is None:
            continue
        existing = db.one("""SELECT batch FROM sheet_membership
                             WHERE person_id=? AND channel=?""", (pid, channel))
        if existing is None:
            db.x("""INSERT INTO sheet_membership
                    (person_id, channel, first_seen_at, batch) VALUES (?,?,?,?)""",
                 (pid, channel, utcnow(), batch))
            new_ids.add(pid)
    return new_ids


def main() -> int:
    db = Database("data/master.sqlite")
    sop = config.sop()
    countries = config.approved_countries()
    wb = Workbook()
    wb.remove(wb.active)
    batch = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    new_rows: list[dict] = []

    # ---- 1 & 2: the two SOP final sheets -------------------------------
    for channel, title in (("linkedin", "LinkedIn Final"),
                           ("instagram", "Instagram Final")):
        cols = sop["sheets"][channel]["columns"]
        rows = []
        for r in db.q("""SELECT s.*, p.full_name, p.title, c.* FROM scores s
                         JOIN people p USING(person_id)
                         JOIN companies c ON c.company_id = p.company_id
                         WHERE s.channel=? AND s.verdict='FINAL PASS'
                           -- Status must be re-checked here as well: this workbook
                           -- builder is a SECOND export path, and a score row
                           -- outlives a later rejection.
                           AND p.status != 'REJECTED'
                           AND c.status = 'QUALIFIED'
                         ORDER BY s.total DESC""", (channel,)):
            prof = {x["channel"]: x["url"] for x in db.q(
                "SELECT channel, url FROM profiles WHERE person_id=?", (r["person_id"],))}
            ev = db.one("""SELECT url FROM company_evidence WHERE company_id=?
                           AND kind='activity' ORDER BY CASE strength
                           WHEN 'strongest' THEN 0 WHEN 'strong' THEN 1 ELSE 2 END
                           LIMIT 1""", (r["company_id"],))
            sig = [x["signal_type"].replace("_", " ") for x in db.q(
                "SELECT DISTINCT signal_type FROM activity_signals WHERE company_id=?",
                (r["company_id"],))]
            row = {
                "Company Name": r["canonical_name"],
                "Company Website": r["website"] or NF,
                "Company LinkedIn URL": r["linkedin_url"] or NF,
                "Company Instagram URL": r["instagram_url"] or NF,
                "Company Type": sop["company_types"].get(r["company_type"], {}).get("label", ""),
                "Country": countries.get(r["country"], r["country"] or ""),
                "Estimated Artist Roster": r["roster_display"] or NF,
                "Roster Evidence URL": r["roster_evidence_url"] or NF,
                "Recent Activity / Intent Signal": ", ".join(sorted(sig)[:4]),
                "Activity Evidence URL": (ev["url"] if ev else NF),
                "Why Company Fits": r["why_company"] or "",
                "Person Name": r["full_name"],
                "Current Job Title": r["title"] or "",
                "Person LinkedIn URL": prof.get("linkedin", NF),
                "Person Instagram URL": prof.get("instagram", NF),
                "Why This Person Fits": r["why_person"] or "",
                "Qualification Score": r["total"],
                "Verification Status": r["verdict"],
            }
            if channel == "instagram":
                row["Instagram Profile Status"] = "VERIFIED"
            row["_person_id"] = r["person_id"]
            rows.append(row)

        fresh = record_membership(db, channel, rows, batch)
        for row in rows:
            if row.get("_person_id") in fresh:
                new_rows.append({**row, "Channel": title})
        add_sheet(wb, title, cols, rows)

    # ---- 3: qualified companies ----------------------------------------
    cols = ["Company Name", "Country", "Company Type", "Estimated Artist Roster",
            "Roster Evidence URL", "Company Website", "Company LinkedIn URL",
            "Company Instagram URL", "Artists Evidenced", "Activity Signals",
            "People Found", "People In Final Sheets"]
    rows = []
    for r in db.q("""SELECT c.*,
                       (SELECT COUNT(*) FROM artists a WHERE a.company_id=c.company_id) arts,
                       (SELECT COUNT(*) FROM activity_signals s WHERE s.company_id=c.company_id) sigs,
                       (SELECT COUNT(*) FROM people p WHERE p.company_id=c.company_id) ppl,
                       (SELECT COUNT(DISTINCT sc.person_id) FROM scores sc
                         JOIN people p2 ON p2.person_id=sc.person_id
                         WHERE p2.company_id=c.company_id AND sc.verdict='FINAL PASS') passed
                     FROM companies c WHERE c.status='QUALIFIED'
                     ORDER BY passed DESC, ppl DESC, c.canonical_name"""):
        rows.append({
            "Company Name": r["canonical_name"],
            "Country": countries.get(r["country"], r["country"] or ""),
            "Company Type": sop["company_types"].get(r["company_type"], {}).get("label", ""),
            "Estimated Artist Roster": r["roster_display"] or NF,
            "Roster Evidence URL": r["roster_evidence_url"] or NF,
            "Company Website": r["website"] or NF,
            "Company LinkedIn URL": r["linkedin_url"] or NF,
            "Company Instagram URL": r["instagram_url"] or NF,
            "Artists Evidenced": r["arts"], "Activity Signals": r["sigs"],
            "People Found": r["ppl"], "People In Final Sheets": r["passed"],
        })
    add_sheet(wb, "Qualified Companies", cols, rows)

    # ---- 4: near-miss people (85-89) -----------------------------------
    cols = ["Person Name", "Current Job Title", "Company Name", "Country", "Channel",
            "Score", "Verdict", "Person LinkedIn URL", "Person Instagram URL",
            "What Is Missing"]
    rows = []
    for r in db.q("""SELECT s.*, p.full_name, p.title, c.canonical_name, c.country
                     FROM scores s JOIN people p USING(person_id)
                     JOIN companies c ON c.company_id=p.company_id
                     WHERE s.verdict='RESEARCH FURTHER' ORDER BY s.total DESC"""):
        prof = {x["channel"]: x["url"] for x in db.q(
            "SELECT channel, url FROM profiles WHERE person_id=?", (r["person_id"],))}
        import json as _json
        gaps = _json.loads(r["failed_gates"] or "[]")
        rows.append({
            "Person Name": r["full_name"], "Current Job Title": r["title"] or "",
            "Company Name": r["canonical_name"],
            "Country": countries.get(r["country"], r["country"] or ""),
            "Channel": r["channel"], "Score": r["total"], "Verdict": r["verdict"],
            "Person LinkedIn URL": prof.get("linkedin", NF),
            "Person Instagram URL": prof.get("instagram", NF),
            "What Is Missing": ", ".join(gaps) if gaps
                               else "Score 85-89: below the SS45 threshold of 90",
        })
    add_sheet(wb, "Near Miss 85-89", cols, rows)

    # ---- 5: all people researched --------------------------------------
    cols = ["Person Name", "Current Job Title", "Company Name", "Role Family",
            "Seniority", "Employment Verified", "Status", "Person LinkedIn URL",
            "Person Instagram URL", "Reject Reason"]
    rows = []
    for r in db.q("""SELECT p.*, c.canonical_name FROM people p
                     JOIN companies c ON c.company_id=p.company_id
                     ORDER BY c.canonical_name, p.full_name"""):
        prof = {x["channel"]: x["url"] for x in db.q(
            "SELECT channel, url FROM profiles WHERE person_id=?", (r["person_id"],))}
        rows.append({
            "Person Name": r["full_name"], "Current Job Title": r["title"] or "",
            "Company Name": r["canonical_name"],
            "Role Family": (r["title_family"] or "").replace("_", " "),
            "Seniority": r["seniority"] or "",
            "Employment Verified": "YES" if r["current_verified"] else "NO",
            "Status": r["status"],
            "Person LinkedIn URL": prof.get("linkedin", NF),
            "Person Instagram URL": prof.get("instagram", NF),
            "Reject Reason": r["reject_reason"] or "",
        })
    add_sheet(wb, "All People Researched", cols, rows)

    # ---- 6: rejected companies -----------------------------------------
    cols = ["Company Name", "Country", "Gate", "Reason", "Rejected At"]
    rows = [{"Company Name": r["canonical_name"],
             "Country": countries.get(r["country"], r["country"] or ""),
             "Gate": r["gate"], "Reason": r["reason_text"],
             "Rejected At": (r["rejected_at"] or "")[:10]}
            for r in db.q("""SELECT c.canonical_name, c.country, r.* FROM company_rejections r
                             JOIN companies c USING(company_id)
                             ORDER BY r.rejected_at DESC""")]
    add_sheet(wb, "Rejected Companies", cols, rows)

    # ---- 7: review queue ------------------------------------------------
    cols = ["Company Name", "Country", "Status", "Estimated Artist Roster",
            "Company Website", "Unresolved Gates"]
    rows = []
    import json as _json
    for r in db.q("""SELECT canonical_name, country, status, roster_display, website,
                            gate_result FROM companies
                     WHERE status IN ('REVIEW','UNRESOLVED') ORDER BY canonical_name"""):
        g = _json.loads(r["gate_result"] or "{}")
        gaps = [f"{k} {v['name']}: {v['reason'][:90]}" for k, v in g.items()
                if v["result"] != "PASS"]
        rows.append({"Company Name": r["canonical_name"],
                     "Country": countries.get(r["country"], r["country"] or ""),
                     "Status": r["status"],
                     "Estimated Artist Roster": r["roster_display"] or NF,
                     "Company Website": r["website"] or NF,
                     "Unresolved Gates": " | ".join(gaps) or "domain unresolved"})
    add_sheet(wb, "Review Queue", cols, rows)

    # Atomic write: save to a temp file in the same directory, then replace. A crash
    # or a kill mid-save can therefore never leave a truncated/corrupt workbook, and
    # the previous good file survives. os.replace is atomic on the same filesystem.
    out = Path("Music_Industry_Prospects.xlsx")
    tmp = out.with_suffix(".xlsx.tmp")
    wb.save(tmp)
    os.replace(tmp, out)

    # Dated snapshot so history is never lost, even if the live file is overwritten
    # or held open by Excel.
    archive = Path("runs/_workbook_archive")
    archive.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copy2(out, archive / f"prospects_{stamp}.xlsx")
    # Keep the 20 most recent snapshots.
    snaps = sorted(archive.glob("prospects_*.xlsx"))
    for old in snaps[:-20]:
        old.unlink()
    db.close()

    print(f"written: {out.resolve()}")
    for ws in wb.worksheets:
        print(f"   {ws.title:26s} {ws.max_row - 1:5d} rows x {ws.max_column} cols")
    print(f"\ngenerated {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
