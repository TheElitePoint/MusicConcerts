"""Discogs -- label identity, roster corroboration and catalogue recency (SS20).

Three things Discogs supplies better than anything else free:
  * the label's own external URLs, including the official site -> S2 resolution;
  * distinct artists across RECENT releases -> an active-roster estimate (SS6);
  * the latest release year -> current-activity evidence (SS27).

SS20 is explicit: "Do not qualify a company solely because it has an old Discogs
catalogue. Current activity must still be verified." So roster and activity are
derived only from releases inside a recency window, never from the all-time
catalogue depth, and the result is always expressed as an approximate RANGE.
"""
from __future__ import annotations

import re
from datetime import date

from ..http import HttpClient
from ..normalize import normalize_company_name

BASE = "https://api.discogs.com"
PROVIDER = "discogs"

# Discogs disambiguates same-named artists as "Kneecap (2)" and marks
# name-variants with a trailing asterisk.
_DISAMBIG = re.compile(r"\s*\(\d+\)\s*$")
_VARIANT = re.compile(r"\*+$")

# A "various artists" style credit is not a roster member.
_NON_ARTIST = {"various", "various artists", "unknown artist", "no artist", "v/a"}


def _clean_artist(name: str) -> str:
    name = _VARIANT.sub("", _DISAMBIG.sub("", (name or "").strip())).strip()
    # Discogs joins collaborations into one credit string; a very long credit is a
    # collaboration line, not a single roster artist.
    if not name or name.casefold() in _NON_ARTIST or len(name) > 60:
        return ""
    return name


def _token(http: HttpClient) -> str:
    return http.ledger.key_for(PROVIDER)


def search_label(http: HttpClient, name: str, limit: int = 5) -> list[dict]:
    """Find candidate labels by name. Exact-ish matches only, ranked."""
    token = _token(http)
    if not token:
        return []
    resp = http.get(f"{BASE}/database/search", provider=PROVIDER,
                    params={"q": name, "type": "label", "per_page": limit,
                            "token": token},
                    context=f"search:{name}", cache_exclude=("token",))
    if not resp.ok:
        return []

    target = normalize_company_name(name)
    out = []
    for item in (resp.json() or {}).get("results", []):
        title = item.get("title", "")
        out.append({"id": item.get("id"), "name": title,
                    "url": f"https://www.discogs.com{item.get('uri', '')}",
                    "exact": normalize_company_name(title) == target})
    # Prefer an exact normalized-name match over Discogs' own relevance order.
    out.sort(key=lambda r: (not r["exact"],))
    return out


def label_detail(http: HttpClient, label_id: int | str) -> dict:
    """Label profile: external URLs (incl. official site), sublabels, description."""
    token = _token(http)
    if not token or not label_id:
        return {}
    resp = http.get(f"{BASE}/labels/{label_id}", provider=PROVIDER,
                    params={"token": token}, context=f"label:{label_id}",
                    cache_exclude=("token",))
    if not resp.ok:
        return {}
    data = resp.json() or {}
    return {
        "id": data.get("id"),
        "name": data.get("name", ""),
        "profile": (data.get("profile") or "")[:2000],
        "urls": data.get("urls") or [],
        "sublabels": [s.get("name", "") for s in (data.get("sublabels") or [])],
        "evidence_url": data.get("uri") or f"https://www.discogs.com/label/{label_id}",
    }


def official_site(detail: dict) -> str:
    """Pick the label's own website from its Discogs URL list."""
    social = ("facebook.", "twitter.", "instagram.", "youtube.", "bandcamp.",
              "soundcloud.", "spotify.", "myspace.", "discogs.", "wikipedia.",
              "last.fm", "linktr.ee", "x.com")
    for url in detail.get("urls") or []:
        low = url.lower()
        if low.startswith("http") and not any(s in low for s in social):
            return url
    return ""


def label_activity(http: HttpClient, label_id: int | str, *,
                   recent_years: int = 2, pages: int = 1) -> dict:
    """Active roster and release recency from the label's most recent releases.

    Returns {artists, artist_count, recent_release_count, latest_year,
             total_releases, evidence_url}.

    Only releases within `recent_years` feed the artist set, so a dormant label with
    a deep back-catalogue does not present as an active roster (SS20).
    """
    token = _token(http)
    if not token or not label_id:
        return {}

    cutoff = date.today().year - recent_years
    artists: list[str] = []
    seen: set[str] = set()
    recent = 0
    latest = 0
    total = 0

    for page in range(1, pages + 1):
        resp = http.get(f"{BASE}/labels/{label_id}/releases", provider=PROVIDER,
                        params={"per_page": 100, "page": page, "sort": "year",
                                "sort_order": "desc", "token": token},
                        context=f"releases:{label_id}:{page}",
                        cache_exclude=("token",))
        if not resp.ok:
            break
        data = resp.json() or {}
        total = (data.get("pagination") or {}).get("items", total)

        for item in data.get("releases", []):
            year = item.get("year") or 0
            try:
                year = int(year)
            except (TypeError, ValueError):
                year = 0
            latest = max(latest, year)
            if year and year >= cutoff:
                recent += 1
                name = _clean_artist(item.get("artist", ""))
                key = name.casefold()
                if name and key not in seen:
                    seen.add(key)
                    artists.append(name)
        if len(data.get("releases", [])) < 100:
            break

    return {
        "artists": artists,
        "artist_count": len(artists),
        "recent_release_count": recent,
        "latest_year": latest,
        "total_releases": total,
        "evidence_url": f"https://www.discogs.com/label/{label_id}",
    }
