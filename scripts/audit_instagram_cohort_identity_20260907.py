"""Audit the 200-person Instagram cohort for identity accuracy."""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "data/paired_linkedin_instagram_200_20260907.json"
SNAPSHOT = ROOT / "data/reference/music_data_google_sheet_snapshot_20260907.json"
OUTPUT = ROOT / "data/instagram_identity_audit_20260907.json"
RAW_FILES = [
    ROOT / "data/instagram_runs/apify_profile_verification_20260907T125453Z/run_tcjRgvRiph6jsNP6Q.json",
    ROOT / "data/instagram_runs/apify_profile_verification_20260907T125752Z/run_9jLuUDsCjenJM6lHg.json",
]


def ascii_norm(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def words(value: str | None) -> list[str]:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    return [w for w in re.findall(r"[a-z0-9]+", text) if len(w) >= 4]


def url_key(value: str | None) -> str:
    return (value or "").strip().lower().rstrip("/")


def handle(value: str | None) -> str:
    match = re.search(r"instagram\.com/([^/?#]+)", value or "", re.I)
    return match.group(1).lower() if match else ""


def domain(value: str | None) -> str:
    try:
        return urlparse(value or "").netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def main() -> int:
    payload = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    cohort = payload["instagram_rows"]
    raw_by_handle = {}
    for path in RAW_FILES:
        for row in json.loads(path.read_text(encoding="utf-8")):
            username = (row.get("username") or handle(row.get("url"))).lower()
            if username:
                raw_by_handle[username] = row

    con = sqlite3.connect(ROOT / "data/master.sqlite")
    con.row_factory = sqlite3.Row
    db_by_linkedin = {
        url_key(r["linkedin_url"]): dict(r)
        for r in con.execute(
            """SELECT li.url AS linkedin_url,ig.url AS instagram_url,
                      ig.verification_status,ig.identity_score,ig.match_signals,
                      si.total AS instagram_score,si.verdict AS instagram_verdict
                 FROM profiles li
                 LEFT JOIN profiles ig ON ig.person_id=li.person_id AND ig.channel='instagram'
                 LEFT JOIN scores si ON si.person_id=li.person_id AND si.channel='instagram'
                WHERE li.channel='linkedin'"""
        )
    }
    con.close()

    audited = []
    for row in cohort:
        h = handle(row.get("Person Instagram URL"))
        raw = raw_by_handle.get(h, {})
        db = db_by_linkedin.get(url_key(row.get("Person LinkedIn URL")), {})
        person_norm = ascii_norm(row.get("Person Name"))
        full_norm = ascii_norm(raw.get("fullName"))
        name_exact = bool(person_norm and full_norm and person_norm == full_norm)
        name_ratio = SequenceMatcher(None, person_norm, full_norm).ratio() if full_norm else 0.0
        bio = raw.get("biography") or ""
        external_urls = []
        for item in raw.get("externalUrls") or []:
            if isinstance(item, str):
                external_urls.append(item)
            elif isinstance(item, dict):
                external_urls.append(str(item.get("url") or item.get("lynx_url") or ""))
        blob = " ".join([
            bio,
            " ".join(external_urls),
            raw.get("businessCategoryName") or "",
        ]).lower()
        company_tokens = [w for w in words(row.get("Company Name")) if w not in {"records", "music", "entertainment", "recordings", "group"}]
        website_domain = domain(row.get("Company Website"))
        company_signal = any(token in ascii_norm(blob) for token in company_tokens)
        domain_signal = bool(website_domain and website_domain in blob)
        db_verified = (
            db.get("verification_status") == "VERIFIED"
            and url_key(db.get("instagram_url")) == url_key(row.get("Person Instagram URL"))
        )
        final_pass = db_verified and db.get("instagram_verdict") == "FINAL PASS" and int(db.get("instagram_score") or 0) >= 90

        if final_pass:
            decision = "KEEP FINAL"
            reason = "Passed the stored SOP Instagram identity and FINAL gates."
        elif db_verified and (company_signal or domain_signal):
            decision = "KEEP REVIEW"
            reason = "Stored identity verification plus a current-company signal; below full FINAL readiness."
        elif name_exact and (company_signal or domain_signal):
            decision = "KEEP REVIEW"
            reason = "Exact displayed-name match and public bio/domain company signal; needs full SOP corroboration."
        elif name_exact:
            decision = "UNRESOLVED"
            reason = "Exact displayed name only; no independent current-company relationship signal."
        elif name_ratio >= 0.90 and (company_signal or domain_signal):
            decision = "KEEP REVIEW"
            reason = "Near-exact displayed name plus public company signal; needs full SOP corroboration."
        else:
            decision = "REJECT CANDIDATE"
            reason = "Displayed identity is missing/mismatched or lacks a reliable link to the verified prospect."
        audited.append({
            "Company Name": row.get("Company Name", ""),
            "Person Name": row.get("Person Name", ""),
            "Current Job Title": row.get("Current Job Title", ""),
            "Person LinkedIn URL": row.get("Person LinkedIn URL", ""),
            "Candidate Instagram URL": row.get("Person Instagram URL", ""),
            "IG Display Name": raw.get("fullName", ""),
            "IG Bio": bio,
            "Name Exact": name_exact,
            "Name Similarity": round(name_ratio, 3),
            "Company Signal": company_signal,
            "Company Domain Signal": domain_signal,
            "Stored Verification": db.get("verification_status") or "",
            "Stored Identity Score": db.get("identity_score") if db.get("identity_score") is not None else "",
            "Decision": decision,
            "Reason": reason,
        })

    counts = {}
    for row in audited:
        counts[row["Decision"]] = counts.get(row["Decision"], 0) + 1
    OUTPUT.write_text(json.dumps({"counts": counts, "rows": audited}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"counts": counts, "output": str(OUTPUT)}))
    for row in audited:
        if row["Decision"] in {"REJECT CANDIDATE", "UNRESOLVED"}:
            print(json.dumps({k: row[k] for k in ("Person Name", "Company Name", "Candidate Instagram URL", "IG Display Name", "Decision")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
