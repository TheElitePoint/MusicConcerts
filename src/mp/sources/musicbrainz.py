"""MusicBrainz -- the primary keyless discovery and label-identity source.

Why this is stage-one: a release search scoped by date and country returns the
label name inline, so one request yields dozens of currently-active labels in an
approved country. No key, no quota -- only a 1 req/sec politeness limit and a
contact-bearing User-Agent (both enforced by HttpClient).

SS21: "Use as an enrichment and cross-checking source." We use it for both
discovery and identity resolution, but never as sole proof of current activity.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..http import HttpClient
from ..models import RawSignal

BASE = "https://musicbrainz.org/ws/2"
PROVIDER = "musicbrainz"

# Label types that represent real production companies, not pressing plants or
# distribution-only bookkeeping entries.
PRODUCTION_TYPES = {"Original Production", "Production", "Imprint", "Holding"}

# MusicBrainz label names that are placeholders, not companies.
NON_LABELS = {"[no label]", "[unknown]", "[independent]", "none", ""}


def recent_releases(http: HttpClient, country: str, *, lookback_days: int = 120,
                    limit: int = 100, offset: int = 0) -> tuple[list[RawSignal], int]:
    """Releases in `country` within the lookback window, with their labels.

    Implements the SS3 'activity first' rule: we start from real releases and work
    backwards to the company, never from a generic company list.
    """
    until = date.today()
    since = until - timedelta(days=lookback_days)
    query = f"date:[{since.isoformat()} TO {until.isoformat()}] AND country:{country}"
    resp = http.get(f"{BASE}/release", provider=PROVIDER,
                    params={"query": query, "fmt": "json",
                            "limit": min(limit, 100), "offset": offset},
                    context=f"recent_releases:{country}")
    data = resp.json() or {}
    signals: list[RawSignal] = []
    releases = data.get("releases", [])

    for rel in releases:
        rel_id = rel.get("id", "")
        rel_date = rel.get("date", "") or ""
        artists = tuple(
            ac.get("name", "") for ac in (rel.get("artist-credit") or [])
            if isinstance(ac, dict) and ac.get("name")
        )
        for info in rel.get("label-info") or []:
            label = (info.get("label") or {})
            name = (label.get("name") or "").strip()
            if not name or name.lower() in NON_LABELS:
                continue
            signals.append(RawSignal(
                raw_label=name,
                signal_type="release_activity",
                description=f"Release '{rel.get('title', '')}' ({rel_date or 'undated'})"
                            + (f" by {artists[0]}" if artists else ""),
                evidence_url=f"https://musicbrainz.org/release/{rel_id}",
                observed_at=rel_date,
                source=PROVIDER,
                external_id=f"release:{rel_id}:{label.get('id', name)}",
                artists=artists,
                country_hint=country,
                label_external_id=label.get("id", ""),
            ))
    # Returning the RELEASE count separately matters: many releases carry no
    # label-info, so signal count is a poor pagination signal and would stop the
    # crawl early.
    return signals, len(releases)


def search_label(http: HttpClient, name: str, limit: int = 5) -> list[dict]:
    """Find candidate label entities by name. Used in S2 company resolution."""
    resp = http.get(f"{BASE}/label", provider=PROVIDER,
                    params={"query": name, "fmt": "json", "limit": limit},
                    context=f"search_label:{name}")
    data = resp.json() or {}
    out = []
    for lab in data.get("labels", []):
        area = lab.get("area") or {}
        out.append({
            "mbid": lab.get("id", ""),
            "name": lab.get("name", ""),
            "type": lab.get("type", ""),
            "score": lab.get("score", 0),
            "country": lab.get("country") or area.get("iso-3166-1-codes", [""])[0]
                       if area.get("iso-3166-1-codes") else lab.get("country", ""),
            "area": area.get("name", ""),
            "disambiguation": lab.get("disambiguation", ""),
        })
    return out


def label_urls(http: HttpClient, mbid: str) -> dict[str, list[str]]:
    """External URLs attached to a label, keyed by relation type.

    This is the cheapest reliable path to a label's official website -- checked
    before any web search (S2 fallback order).
    """
    if not mbid:
        return {}
    resp = http.get(f"{BASE}/label/{mbid}", provider=PROVIDER,
                    params={"inc": "url-rels", "fmt": "json"},
                    context=f"label_urls:{mbid}")
    data = resp.json() or {}
    urls: dict[str, list[str]] = {}
    for rel in data.get("relations", []):
        rel_type = rel.get("type", "")
        resource = (rel.get("url") or {}).get("resource", "")
        if resource:
            urls.setdefault(rel_type, []).append(resource)
    return urls


def label_artists(http: HttpClient, mbid: str, limit: int = 100) -> list[str]:
    """Distinct artists with releases on this label -- roster corroboration (SS6).

    Uses the BROWSE endpoint (?label=<mbid>), not the search index. The search index
    keys `label:` on the label NAME, so passing an MBID there silently returns zero
    results; browse resolves the MBID exactly and is the correct API for
    "releases on this label".

    Not authoritative on its own: MusicBrainz reflects catalogue history, and SS20
    warns against qualifying a company on an old catalogue alone. Callers therefore
    render this as an approximate RANGE, never an exact roster figure.
    """
    if not mbid:
        return []
    resp = http.get(f"{BASE}/release", provider=PROVIDER,
                    params={"label": mbid, "inc": "artist-credits",
                            "fmt": "json", "limit": min(limit, 100)},
                    context=f"label_artists:{mbid}")
    data = resp.json() or {}
    names: list[str] = []
    seen: set[str] = set()
    for rel in data.get("releases", []):
        for ac in rel.get("artist-credit") or []:
            name = ac.get("name", "") if isinstance(ac, dict) else ""
            key = name.lower().strip()
            if name and key not in seen:
                seen.add(key)
                names.append(name)
    return names


def official_homepage(urls: dict[str, list[str]]) -> str:
    """Pick the best official site from label url-rels, in descending trust order."""
    for rel_type in ("official homepage", "official site", "homepage"):
        if urls.get(rel_type):
            return urls[rel_type][0]
    return ""


# MusicBrainz stores a label's location as a free-form area (a city, a subdivision,
# or a country) rather than an ISO code, so a city has to be walked up its
# `part-of` chain until an area carries an ISO-3166-1 code.
def _area_iso(http: HttpClient, area_id: str, depth: int = 0) -> str:
    if not area_id or depth > 3:
        return ""
    resp = http.get(f"{BASE}/area/{area_id}", provider=PROVIDER,
                    params={"inc": "area-rels", "fmt": "json"},
                    context=f"area:{area_id}")
    data = resp.json() or {}
    codes = data.get("iso-3166-1-codes") or []
    if codes:
        return str(codes[0]).upper()
    # Walk outward: "London" -> "England" -> "United Kingdom" (which carries GB).
    for rel in data.get("relations", []):
        if rel.get("type") == "part of" and rel.get("direction") == "backward":
            parent = (rel.get("area") or {}).get("id", "")
            iso = _area_iso(http, parent, depth + 1)
            if iso:
                return iso
    return ""


def label_country(http: HttpClient, mbid: str) -> tuple[str, str]:
    """(ISO-2, evidence_url) for a label, from its MusicBrainz area.

    The label endpoint's own `country` field is null far more often than its `area`
    is (measured: 0/10 vs 5/10 on the REVIEW backlog), so the area is the usable
    signal. SS7 treats this as one established source, not a licence to skip
    corroboration.
    """
    if not mbid:
        return "", ""
    resp = http.get(f"{BASE}/label/{mbid}", provider=PROVIDER,
                    params={"fmt": "json"}, context=f"label_country:{mbid}")
    data = resp.json() or {}
    url = f"https://musicbrainz.org/label/{mbid}"
    direct = (data.get("country") or "").upper()
    if direct:
        return direct, url
    area = data.get("area") or {}
    if area.get("id"):
        iso = _area_iso(http, area["id"])
        if iso:
            return iso, url
    return "", ""


# MusicBrainz label types map onto the SS4 approved-category enum. Types absent
# from this map (e.g. "Holding", "Bootleg Production") are deliberately unmapped:
# guessing a category is what SS4 forbids.
MB_TYPE_TO_SOP = {
    "Original Production": "record_label",
    "Reissue Production": "record_label",
    "Production": "record_label",
    "Imprint": "record_label",
    "Distributor": "music_distributor",
}
# Deliberately unmapped: "Publisher" -- music publishing is not one of the five
# SS4 approved categories, so it must reach the gate as unestablished, not as a
# category invented to make G2 pass.


def label_type(http: HttpClient, mbid: str) -> tuple[str, str]:
    """(SS4 category, evidence_url) from the MusicBrainz label type, if mappable."""
    if not mbid:
        return "", ""
    resp = http.get(f"{BASE}/label/{mbid}", provider=PROVIDER,
                    params={"fmt": "json"}, context=f"label_type:{mbid}")
    data = resp.json() or {}
    mapped = MB_TYPE_TO_SOP.get(data.get("type") or "", "")
    return (mapped, f"https://musicbrainz.org/label/{mbid}") if mapped else ("", "")
