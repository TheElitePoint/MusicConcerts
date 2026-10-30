"""Build an exact LinkedIn organization directory from official public pages.

For QUALIFIED companies missing a LinkedIn URL, the script accepts a company
page only when it is linked from that company's own official website.  It then
opens each public LinkedIn company page, extracts the numeric organization ID
from the visible "all employees" CTA, and records the visible employee profile
links.  No people are admitted to the database here; this is identity-scoped
discovery data for later SOP role and employment verification.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
COMPANY_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[^/?#\"'&]+", re.I)
PERSON_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[^/?#\"'&]+", re.I)
ORG_RE = re.compile(r"facetCurrentCompany(?:%3D|=)(?:%255B|%5B|\[)(\d+)(?:%255D|%5D|\])", re.I)


def _get(url: str) -> tuple[str, str]:
    try:
        response = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=8, allow_redirects=True)
        if response.ok and "text/html" in response.headers.get("content-type", "text/html"):
            return response.url, response.text
    except requests.RequestException:
        pass
    return "", ""


def _linkedin_company_url(value: str) -> str:
    match = COMPANY_RE.search(value or "")
    if not match:
        return ""
    return match.group(0).rstrip("/.,)")


def _official_linkedin(website: str) -> str:
    final, body = _get(website)
    if not body:
        return ""
    soup = BeautifulSoup(body, "lxml")
    for anchor in soup.find_all("a", href=True):
        found = _linkedin_company_url(urljoin(final, anchor.get("href", "")))
        if found:
            return found
    # Some social links are stored in JSON rather than rendered anchors.
    found = _linkedin_company_url(body)
    if found:
        return found

    host = urlparse(final).netloc.lower()
    candidates: list[str] = []
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True).lower()
        href = urljoin(final, anchor.get("href", ""))
        parsed = urlparse(href)
        if parsed.netloc.lower() != host:
            continue
        if any(word in text or word in parsed.path.lower() for word in ("about", "contact", "team", "company")):
            if href not in candidates:
                candidates.append(href)
        if len(candidates) >= 1:
            break
    for page in candidates:
        _, sub = _get(page)
        found = _linkedin_company_url(sub)
        if found:
            return found
    return ""


def _company_page(company_id: int, name: str, linkedin_url: str) -> dict:
    final, body = _get(linkedin_url)
    result = {
        "company_id": company_id,
        "company_name": name,
        "linkedin_url": final or linkedin_url,
        "linkedin_name": "",
        "linkedin_org_id": "",
        "employees": [],
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if not body:
        return result
    soup = BeautifulSoup(body, "lxml")
    heading = soup.find("h1")
    if heading:
        result["linkedin_name"] = heading.get_text(" ", strip=True)
    if not result["linkedin_name"] and soup.title:
        result["linkedin_name"] = soup.title.get_text(" ", strip=True).split("|", 1)[0].strip()
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        match = ORG_RE.search(href)
        if not match and "session_redirect=" in href:
            redirect = parse_qs(urlparse(href).query).get("session_redirect", [""])[0]
            match = re.search(r"facetCurrentCompany=\[(\d+)\]", unquote(redirect), re.I)
        if match:
            result["linkedin_org_id"] = match.group(1)
        person = PERSON_RE.search(href)
        if not person:
            continue
        url = person.group(0).split("?", 1)[0].rstrip("/")
        employee = {"name": anchor.get_text(" ", strip=True), "linkedin_url": url}
        if employee["name"] and employee not in result["employees"]:
            result["employees"].append(employee)
    return result


def main(workers: int, *, skip_backfill: bool = False) -> int:
    db_path = ROOT / "data" / "master.sqlite"
    con = sqlite3.connect(db_path, timeout=60)
    con.row_factory = sqlite3.Row
    missing = [] if skip_backfill else con.execute(
        """SELECT company_id,canonical_name,website FROM companies
            WHERE status='QUALIFIED' AND website IS NOT NULL AND trim(website)!=''
              AND (linkedin_url IS NULL OR trim(linkedin_url)='' OR linkedin_url='Not Found')"""
    ).fetchall()
    print(f"Official-site LinkedIn backfill candidates: {len(missing)}", flush=True)
    found: list[tuple[int, str, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(_official_linkedin, row["website"]): row for row in missing}
        for future in as_completed(jobs):
            row = jobs[future]
            url = future.result()
            if url:
                found.append((row["company_id"], row["canonical_name"], url))
                print(f"  {row['canonical_name']}: {url}", flush=True)
    now = utcnow()
    for company_id, _name, url in found:
        con.execute("UPDATE companies SET linkedin_url=?,last_researched=? WHERE company_id=?", (url, now, company_id))
        con.execute(
            """INSERT INTO company_evidence
               (company_id,kind,url,source,snippet,strength,retrieved_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(company_id,kind,url) DO UPDATE SET
                 source=excluded.source,snippet=excluded.snippet,
                 strength=excluded.strength,retrieved_at=excluded.retrieved_at""",
            (company_id, "profile", url, "official_website_link",
             "Official company website links to this LinkedIn company page", "strongest", now),
        )
    con.commit()
    rows = con.execute(
        """SELECT company_id,canonical_name,linkedin_url FROM companies
            WHERE status='QUALIFIED' AND linkedin_url LIKE '%linkedin.com/company/%'
            ORDER BY company_id"""
    ).fetchall()
    print(f"Public LinkedIn company pages to inspect: {len(rows)}", flush=True)
    directory: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(_company_page, row["company_id"], row["canonical_name"], row["linkedin_url"]): row for row in rows}
        for future in as_completed(jobs):
            entry = future.result()
            directory.append(entry)
            if entry["linkedin_org_id"] or entry["employees"]:
                print(f"  {entry['company_name']}: org={entry['linkedin_org_id'] or '-'} employees={len(entry['employees'])}", flush=True)
    directory.sort(key=lambda item: item["company_id"])
    output = ROOT / "data" / "linkedin_company_directory.json"
    output.write_text(json.dumps(directory, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Backfilled={len(found)} directory={len(directory)} org_ids={sum(bool(x['linkedin_org_id']) for x in directory)} employees={sum(len(x['employees']) for x in directory)} output={output}", flush=True)
    con.close()
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=30)
    parser.add_argument("--skip-backfill", action="store_true")
    args = parser.parse_args()
    raise SystemExit(main(max(1, min(args.workers, 40)), skip_backfill=args.skip_backfill))
