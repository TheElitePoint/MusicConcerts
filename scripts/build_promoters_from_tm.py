"""Turn collected Ticketmaster events into Projects LIVE promoter companies.

Company SOP 01 v4.0 SS4 ("Promoter / Live (6+ Events)"), client rules 2026-10-25:
  * the company must be the promoter of record on 6+ DISTINCT music events in one
    12-month window (here: the next 12 months of published dates);
  * a multi-day festival is ONE event; cancelled shows and duplicate listings of
    the same show do not count;
  * Live Nation and AEG Presents (and the brands they own), ticketing companies,
    venues listed as their own promoter, and Indian companies are excluded.
Every counted event is stored as an activity signal with its Ticketmaster URL --
the per-event promoter proof the SOP requires. Without --write it only writes the
review CSV (data/promoters/promoters_<date>.csv).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import used_contacts  # noqa: E402
from mp.db import utcnow  # noqa: E402
from mp.normalize import loose_company_key, normalize_company_name  # noqa: E402
from mp.pipeline import Context  # noqa: E402

import yaml  # noqa: E402

_REVIEW = yaml.safe_load((ROOT / "config/promoter_review.yaml").read_text())
REVIEW_EXCLUDE = dict(_REVIEW.get("exclude") or {})
MERGE_NAMES = {k: v for k, v in (_REVIEW.get("merge") or {}).items()}
MERGE_KEYS = {k: normalize_company_name(v) for k, v in MERGE_NAMES.items()}
MERGE_NAMES.update({normalize_company_name(v): v for v in MERGE_NAMES.values()})
MIN_EVENTS = 6
FESTIVAL_GAP_DAYS = 4
# Live Nation / AEG and the promoters they own, ticketing, and placeholder promoters.
EXCLUDED = re.compile(
    r"live ?nation|\blnm?\b|\baeg\b|goldenvoice|concerts west|messina touring|ocesa|"
    r"mcd productions|mojo concerts|c3 presents|frontier touring|festival republic|"
    r"academy music group|metropolis music|df concerts|cream\b|insomniac|"
    r"house of blues|ticketmaster|tickets?\b|ticketing|eventim|see tickets|axs\b|dice\b|"
    r"promoted by venue|miscellaneous|^misc|various|\btba\b|\btbc\b|unknown|"
    r"\bvenue\b|not defined|bowery presents|messina group|frank productions|"
    r"venuworks|\bsmg\b|asm global|oak view group|spectra\b|"
    r"comedy|livestock|rodeo|\bexpo\b|ministr|\bfair\b", re.I)
# Places and public bodies listed as promoter of their own building's programme.
VENUE_WORDS = re.compile(
    r"\b(arena|dome|stadium|stadion|theat(?:er|re)|teatro|theatre|hall|halle|kulturhus|"
    r"konserthus|concert ?hall|center|centre|auditorium|amphitheat\w*|opera|"
    r"philharmoni\w*|symphony|orchestra|orkest\w*|casino|club\b|bar\b|pub\b|church|"
    r"kirke\w*|kommune|council|city of|university|museum|park\b|pavilion|forum|"
    r"visit\b)", re.I)


def norm_event(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def distinct_events(events: list[dict]) -> list[dict]:
    """Collapse duplicate listings and multi-day festivals; keep one row per event."""
    by_key: dict[tuple, dict] = {}
    for e in events:
        if (e.get("status") or "").lower() in ("cancelled", "canceled", "postponed"):
            continue
        if not e.get("date"):
            continue
        by_key.setdefault((norm_event(e["name"]), e["date"]), e)
    out: list[dict] = []
    last: dict[str, date] = {}
    for e in sorted(by_key.values(), key=lambda x: (norm_event(x["name"]), x["date"])):
        d = date.fromisoformat(e["date"])
        name = norm_event(e["name"])
        if name in last and (d - last[name]).days <= FESTIVAL_GAP_DAYS and \
                re.search(r"fest|festival|weekend|day \d", name):
            last[name] = d          # same festival, another day
            continue
        last[name] = d
        out.append(e)
    return out


def display_name(raw: str) -> str:
    return raw.title() if raw.isupper() else raw


def main(path: Path, write: bool) -> int:
    events = [json.loads(l) for l in path.open(encoding="utf-8")]
    grouped: dict[str, list[dict]] = defaultdict(list)
    names: dict[str, Counter] = defaultdict(Counter)
    for e in events:
        p = e.get("promoter") or {}
        if not p.get("name"):
            continue
        key = normalize_company_name(p["name"])
        key = MERGE_KEYS.get(key, key)
        grouped[key].append(e)
        names[key][p["name"]] += 1

    today = date.today()
    horizon = (today + timedelta(days=365)).isoformat()
    rows, rejects = [], Counter()
    for key, evs in grouped.items():
        raw = MERGE_NAMES.get(key) or names[key].most_common(1)[0][0]
        if any(key.startswith(x) or x in key for x in REVIEW_EXCLUDE):
            rejects["manual review exclusion"] += 1
            continue
        if EXCLUDED.search(raw):
            rejects["live nation / aeg / ticketing / placeholder"] += 1
            continue
        if VENUE_WORDS.search(raw):
            rejects["venue or public body"] += 1
            continue
        evs = [e for e in evs if today.isoformat() <= (e.get("date") or "") <= horizon]
        uniq = distinct_events(evs)
        if len(uniq) < MIN_EVENTS:
            rejects["fewer than 6 events"] += 1
            continue
        venues = Counter(e.get("venue") for e in uniq)
        top_venue, top_n = venues.most_common(1)[0]
        if top_n / len(uniq) >= 0.8 and top_venue and \
                set(loose_company_key(raw).split()) & set(loose_company_key(top_venue).split()):
            rejects["in-house venue programme"] += 1
            continue
        # One city and at most two rooms, with no promoter wording in the name: a bar or
        # club programming its own stage (VENUE_ONLY), not a concert promoter.
        cities = {e.get("city") for e in uniq}
        if len(cities) == 1 and len(venues) <= 2 and not re.search(
                r"present|production|concert|promot|entertainment|live|events|touring|agency",
                raw, re.I):
            rejects["single-venue programme"] += 1
            continue
        country = Counter(e["country"] for e in uniq).most_common(1)[0][0]
        if country == "IN":
            rejects["india"] += 1
            continue
        if used_contacts.company_blocked(raw):
            rejects["used-contacts list"] += 1
            continue
        rows.append({"promoter": display_name(raw), "key": key, "country": country,
                     "events": len(uniq), "cities": len({e.get("city") for e in uniq}),
                     "venues": len(venues), "first": min(e["date"] for e in uniq),
                     "last": max(e["date"] for e in uniq),
                     "sample": " | ".join(f"{e['date']} {e['name'][:40]} @ {e.get('venue')}"
                                          for e in uniq[:3]),
                     "_events": uniq})
    rows.sort(key=lambda r: -r["events"])

    out = ROOT / f"data/promoters/promoters_{today:%Y%m%d}.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[k for k in rows[0] if not k.startswith("_")] if rows else ["promoter"])
        w.writeheader()
        for r in rows:
            w.writerow({k: v for k, v in r.items() if not k.startswith("_")})
    print(f"{len(rows)} promoters with {MIN_EVENTS}+ events -> {out}")
    print("excluded:", dict(rejects))
    print("by country:", Counter(r["country"] for r in rows).most_common())

    if write:
        ctx = Context.create(db_path="data/master.sqlite")
        created = existing = 0
        try:
            for r in rows:
                cid = ctx.db.alias_lookup(r["key"])
                if cid is None:
                    found = ctx.db.one("SELECT company_id FROM companies WHERE name_normalized=?",
                                       (r["key"],))
                    cid = found["company_id"] if found else None
                if cid is None:
                    cur = ctx.db.x("""INSERT INTO companies
                        (canonical_name, name_normalized, name_loose, country, country_confidence,
                         company_type, type_confidence, roster_estimate, roster_display,
                         roster_evidence_url, status, first_seen, last_researched,
                         discovered_via, description)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (r["promoter"], r["key"], loose_company_key(r["promoter"]), r["country"],
                         "single_source", "concert_festival", "strong", r["events"],
                         f"{r['events']} promoted events (next 12 months)",
                         r["_events"][0]["url"], "QUALIFIED", utcnow(), utcnow(),
                         "ticketmaster_promoter",
                         "Promoter / Live (6+ Events): promoter of record on "
                         f"{r['events']} distinct music events in {r['cities']} cities, "
                         f"{r['first']} to {r['last']}."))
                    cid = cur.lastrowid
                    created += 1
                else:
                    existing += 1
                ctx.db.add_alias(r["key"], cid, r["promoter"], "ticketmaster")
                # SS9 evidence trail: the promoter-of-record event listings (type and
                # activity proof) as company evidence, alongside each event signal.
                for kind, ev in (("type", r["_events"][0]), ("activity", r["_events"][-1])):
                    ctx.db.x("""INSERT OR IGNORE INTO company_evidence
                        (company_id, kind, url, source, snippet, strength, retrieved_at)
                        VALUES (?,?,?,?,?,?,?)""",
                        (cid, kind, ev["url"], "ticketmaster_promoter",
                         f"Promoter of record: {ev['name']} @ {ev.get('venue')}, "
                         f"{ev.get('city')} on {ev['date']} ({r['events']} events in window)",
                         "strong", utcnow()))
                for e in r["_events"]:
                    ctx.db.x("""INSERT OR IGNORE INTO activity_signals
                        (company_id, raw_label, signal_type, description, evidence_url,
                         observed_at, recorded_at, source, external_id)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                        (cid, r["promoter"], "live_event",
                         f"{e['name']} @ {e.get('venue')}, {e.get('city')} ({e['country']})",
                         e["url"], e["date"], utcnow(), "ticketmaster", e["event_id"]))
        finally:
            ctx.close()
        print(f"written: {created} new companies, {existing} already known")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    raise SystemExit(main(a.path, a.write))
