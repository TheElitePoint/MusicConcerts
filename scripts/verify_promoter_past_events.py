"""Count each promoter's concerts in the PAST 12 months from its own website history.

Ticketmaster only lists current and future events. A promoter's own "events" /
"shows" page is first-party attribution (Company SOP 01 SS9 grade A), and the
Internet Archive keeps dated copies of it. For each promoter we:
  1. find its event-listing pages from the live homepage;
  2. take up to one archived copy per month from the last 12 months (CDX API);
  3. extract (event name, date) pairs from each copy -- the model may only return
     events whose name appears verbatim in the page text and whose date is explicit
     (or, without a year, falls inside the 12 months after the snapshot);
  4. keep distinct events dated in the past 12 months and store each as a
     `live_event` signal whose evidence URL is the archived page.
Free: archive.org + the pipeline's Groq classifier.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import warnings

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.sources import groq  # noqa: E402

CACHE = ROOT / "data/cache/wayback"
UA = {"User-Agent": "Mozilla/5.0 (research; music-industry prospect pipeline)"}
EVENT_LINK = re.compile(
    r"event|show|gig|concert|ticket|whats-?on|listing|tour|calendar|agenda|konzert|termine|"
    r"evenement|wydarzen|koncert|programm|lineup|line-up|past|archive", re.I)
TODAY = date.today()
START = TODAY - timedelta(days=365)


def get(url: str, timeout: int = 40) -> str:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / hashlib.sha256(url.encode()).hexdigest()
    if path.exists():
        return path.read_text(encoding="utf-8", errors="ignore")
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200:
                path.write_text(r.text, encoding="utf-8")
                time.sleep(1.0)
                return r.text
            if r.status_code in (429, 503):
                time.sleep(10 * (attempt + 1))
                continue
            return ""
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
    return ""


def event_pages(website: str) -> list[str]:
    html = get(website)
    if not html:
        return [website]
    soup = BeautifulSoup(html, "lxml")
    host = urlparse(website).netloc.replace("www.", "")
    found: list[str] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(website, a["href"]).split("#")[0]
        if host not in urlparse(href).netloc.replace("www.", ""):
            continue
        if EVENT_LINK.search(urlparse(href).path) or EVENT_LINK.search(a.get_text(" ", strip=True)):
            if href.rstrip("/") not in [f.rstrip("/") for f in found]:
                found.append(href)
    # Listing pages first (short paths), never individual event pages.
    found.sort(key=lambda u: len(urlparse(u).path))
    pages, seen = [], set()
    for u in [website] + found:
        k = urlparse(u).netloc.replace("www.", "") + urlparse(u).path.rstrip("/")
        if k not in seen and len(urlparse(u).path.strip("/").split("/")) <= 1:
            seen.add(k)
            pages.append(u)
    return pages[:3]


def detail_prefixes(website: str) -> list[str]:
    """Per-show page prefixes, e.g. kilimanjaro.live/tickets/ -- sites that list only
    artist names on the homepage keep the dates on these pages."""
    html = get(website)
    if not html:
        return []
    host = urlparse(website).netloc.replace("www.", "")
    counts: dict[str, int] = {}
    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
        u = urlparse(urljoin(website, a["href"]))
        segs = [x for x in u.path.split("/") if x]
        if host in u.netloc.replace("www.", "") and len(segs) >= 2 and EVENT_LINK.search(segs[0]):
            pref = f"{host}/{segs[0]}/"
            counts[pref] = counts.get(pref, 0) + 1
    return [p for p, n in sorted(counts.items(), key=lambda x: -x[1]) if n >= 3][:1]


def detail_snapshots(prefix: str, cap: int = 40) -> list[tuple[str, str]]:
    q = ("https://web.archive.org/cdx/search/cdx?url=" + requests.utils.quote(prefix, safe="") +
         f"&matchType=prefix&from={START:%Y%m%d}&to={TODAY:%Y%m%d}&output=json"
         f"&filter=statuscode:200&collapse=urlkey&limit={cap}")
    raw = get(q, timeout=90)
    try:
        return [(r[1], r[2]) for r in json.loads(raw)[1:]] if raw else []
    except ValueError:
        return []


def snapshots(url: str) -> list[str]:
    q = ("https://web.archive.org/cdx/search/cdx?url=" + requests.utils.quote(url, safe="") +
         f"&from={START:%Y%m%d}&to={TODAY:%Y%m%d}&output=json&filter=statuscode:200"
         "&collapse=timestamp:6&limit=14")
    raw = get(q, timeout=60)
    try:
        rows = json.loads(raw)[1:] if raw else []
    except ValueError:
        return []
    return [r[1] for r in rows]


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def extract(ctx: Context, promoter: str, text: str, snap: date) -> list[tuple[str, date]]:
    system = ("You extract concert listings from a music promoter's web page. Return JSON "
              '{"events": [{"name": <exact text from page>, "date": "YYYY-MM-DD"}]}. Only '
              "include live MUSIC events (concerts, gigs, music festivals) this page lists -- "
              "exclude comedy, theatre, talks, sport and family shows. The name must be copied exactly "
              "from the page. Use only dates written on the page; if a listing has no year, "
              f"assume the next occurrence on or after {snap.isoformat()}. Skip anything "
              "without a date. Never invent events.")
    # Paced and bounded: Groq's per-minute limit trips the provider for the whole run
    # after a few 429s (every later page then silently yields nothing), and very
    # long pages are refused outright (413).
    raw = groq._chat(ctx.http, system, f"Promoter: {promoter}\nPage text:\n{text[:8000]}",
                     context=f"past-events:{promoter}:{snap}", max_tokens=3000, tier="strong")
    time.sleep(2.5)
    try:
        items = json.loads(raw[raw.index("{"):raw.rindex("}") + 1]).get("events", [])
    except (ValueError, AttributeError):
        return []
    low = text.lower()
    out = []
    for it in items:
        name = str(it.get("name", "")).strip()
        try:
            d = date.fromisoformat(str(it.get("date", ""))[:10])
        except ValueError:
            continue
        if len(name) < 3 or name.lower()[:20] not in low:
            continue                              # not verbatim on the page
        out.append((name, d))
    return out


def main(limit: int, only: list[int]) -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        sql = """SELECT company_id, canonical_name, website FROM companies
                 WHERE company_type='concert_festival' AND discovered_via='ticketmaster_promoter'
                   AND website IS NOT NULL"""
        rows = ctx.db.q(sql + (f" AND company_id IN ({','.join('?' * len(only))})" if only else "") +
                        " ORDER BY roster_estimate DESC LIMIT ?", (*only, limit))
        for r in rows:
            events: dict[tuple[str, str], str] = {}
            jobs = [(ts, page) for page in event_pages(r["website"]) for ts in snapshots(page)]
            for prefix in detail_prefixes(r["website"]):
                jobs += detail_snapshots(prefix)
            for ts, page in jobs:
                if True:
                    snap = datetime.strptime(ts[:8], "%Y%m%d").date()
                    html = get(f"https://web.archive.org/web/{ts}id_/{page}")
                    if not html:
                        continue
                    for name, d in extract(ctx, r["canonical_name"], page_text(html), snap):
                        if START <= d < TODAY:
                            key = (re.sub(r"[^a-z0-9]+", " ", name.lower()).strip(), d.isoformat())
                            events.setdefault(key, f"https://web.archive.org/web/{ts}/{page}")
            # A multi-day festival is one event: collapse same name within 4 days.
            distinct, last = [], {}
            for (name, d), url in sorted(events.items()):
                dd = date.fromisoformat(d)
                if name in last and (dd - last[name]).days <= 4:
                    last[name] = dd
                    continue
                last[name] = dd
                distinct.append((name, d, url))
            for name, d, url in distinct:
                ctx.db.x("""INSERT OR IGNORE INTO activity_signals
                    (company_id, raw_label, signal_type, description, evidence_url, observed_at,
                     recorded_at, source, external_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (r["company_id"], r["canonical_name"], "live_event", f"{name} (archived listing)",
                     url, d, utcnow(), "wayback_promoter_site",
                     f"wb:{r['company_id']}:{name}:{d}"))
            print(f"{r['canonical_name'][:36]:36s} past-12-month events: {len(distinct)}", flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("company_ids", nargs="*", type=int)
    a = ap.parse_args()
    raise SystemExit(main(a.limit, a.company_ids))
