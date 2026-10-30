"""Collect the next 12 months of music events per country from the Ticketmaster
Discovery API, keeping each event's promoter of record.

Projects LIVE lane (Company SOP 01 v4.0 SS4): a promoter qualifies on 6+ distinct
events it promoted inside one declared 12-month window. Ticketmaster's `promoter`
field is the promoter attached to the event record, so each event URL is
per-event promoter proof. The API pages at most 1,000 results per query, so each
country is queried month by month, and a month over the cap is split into weeks.
Raw events land in data/promoters/tm_events_<date>.jsonl.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config  # noqa: E402

API = "https://app.ticketmaster.com/discovery/v2/events.json"
# Ticketmaster markets. India is excluded by client instruction (2026-10-25).
COUNTRIES = ["US", "CA", "MX", "GB", "IE", "ES", "PT", "DE", "NL", "BE", "LU", "AT", "CH",
             "SE", "NO", "DK", "FI", "IS", "PL", "CZ", "AU", "NZ", "ZA", "AE", "TR", "BR", "CL"]
OUT = ROOT / f"data/promoters/tm_events_{date.today():%Y%m%d}{{suffix}}.jsonl"


def window(start: date, days: int) -> tuple[str, str]:
    a = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    b = a + timedelta(days=days) - timedelta(seconds=1)
    return a.strftime("%Y-%m-%dT%H:%M:%SZ"), b.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(key: str, params: dict) -> dict:
    for attempt in range(5):
        r = requests.get(API, params={**params, "apikey": key}, timeout=30)
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        time.sleep(0.22)                      # stay under 5 requests/second
        return r.json()
    raise RuntimeError("rate limited")


def slice_events(key: str, country: str, start: date, days: int, out) -> tuple[int, int]:
    a, b = window(start, days)
    base = {"classificationName": "music", "countryCode": country, "size": 200,
            "startDateTime": a, "endDateTime": b, "sort": "date,asc"}
    first = fetch(key, {**base, "page": 0})
    total = first.get("page", {}).get("totalElements", 0)
    if total > 1000 and days > 2:
        # Over the deep-paging cap: split the window and recurse.
        half = days // 2
        n1, c1 = slice_events(key, country, start, half, out)
        n2, c2 = slice_events(key, country, start + timedelta(days=half), days - half, out)
        return n1 + n2, c1 + c2 + 1
    saved, calls, page = 0, 1, first
    pages = min(5, (total + 199) // 200)
    for p in range(pages):
        if p:
            page = fetch(key, {**base, "page": p})
            calls += 1
        for e in (page.get("_embedded") or {}).get("events", []):
            venue = ((e.get("_embedded") or {}).get("venues") or [{}])[0]
            out.write(json.dumps({
                "event_id": e.get("id"), "name": e.get("name"), "url": e.get("url"),
                "date": (e.get("dates") or {}).get("start", {}).get("localDate"),
                "status": (e.get("dates") or {}).get("status", {}).get("code"),
                "country": country, "city": (venue.get("city") or {}).get("name"),
                "venue": venue.get("name"), "venue_id": venue.get("id"),
                "promoter": e.get("promoter"), "promoters": e.get("promoters") or [],
                "segment": ((e.get("classifications") or [{}])[0].get("segment") or {}).get("name"),
                "genre": ((e.get("classifications") or [{}])[0].get("genre") or {}).get("name"),
            }, ensure_ascii=False) + "\n")
            saved += 1
    return saved, calls


def main() -> int:
    countries = [c.upper() for c in sys.argv[1:]] or COUNTRIES
    out_path = Path(str(OUT).replace("{suffix}", "" if not sys.argv[1:] else "_" + "-".join(countries[:3])))
    key = config.env("TICKETMASTER_API_KEY")
    if not key:
        raise SystemExit("TICKETMASTER_API_KEY missing from .env")
    today = date.today()
    calls_total = 0
    with out_path.open("w", encoding="utf-8") as out:
        for country in countries:
            saved = 0
            for m in range(12):
                start = today + timedelta(days=30 * m)
                n, c = slice_events(key, country, start, 30 if m < 11 else 35, out)
                saved += n
                calls_total += c
            print(f"{country}: {saved} events ({calls_total} calls so far)", flush=True)
    print(f"done -> {out_path} ({calls_total} API calls)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
