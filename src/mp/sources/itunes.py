"""Apple/iTunes Search API -- keyless label extraction and cross-source confirmation.

The `copyright` field on an album ("(P) 2013 Domino Recording Co Ltd") names the
label directly. Two uses:

  1. DISCOVERY: genre/term searches scoped to an approved country, filtered by
     release date, yield recently-released albums and their labels.
  2. CROSS-SOURCE CONFIRMATION (SS41 B3, worth 5 points): independently corroborating
     a label found via MusicBrainz. B3 requires "at least two credible sources", and
     this is the cheapest second source available -- it costs nothing.

The copyright line also exposes parent-company text ("a division of Sony Music
Entertainment"), which the SS9 exclusion gate needs, so the FULL line is preserved
alongside the extracted label name.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from ..http import HttpClient
from ..models import RawSignal

SEARCH = "https://itunes.apple.com/search"
LOOKUP = "https://itunes.apple.com/lookup"
PROVIDER = "itunes"

# Leading phonogram/copyright mark and year: "(P) 2013 ", "© 2020 ", "(C)2019 "
_LEAD = re.compile(r"^[\s℗©\(\)PCc]*\s*(?:19|20)\d{2}\s*", re.UNICODE)
# Text that introduces a PARENT company rather than the label itself.
_PARENT_SPLITS = re.compile(
    r",?\s+(?:a division of|a subsidiary of|under exclusive licen[cs]e to|"
    r"under licen[cs]e to|distributed by|marketed by|an? .{0,20} company)\s+",
    re.IGNORECASE)

# ISO-3 -> ISO-2 for the countries the SOP approves (iTunes reports ISO-3).
_ISO3_TO_2 = {
    "USA": "US", "CAN": "CA", "MEX": "MX", "GBR": "GB", "IRL": "IE", "ESP": "ES",
    "PRT": "PT", "FRA": "FR", "DEU": "DE", "NLD": "NL", "BEL": "BE", "LUX": "LU",
    "CHE": "CH", "AUT": "AT", "ITA": "IT", "SWE": "SE", "NOR": "NO", "DNK": "DK",
    "FIN": "FI", "ISL": "IS", "POL": "PL", "CZE": "CZ", "EST": "EE", "SVN": "SI",
    "HRV": "HR", "GRC": "GR", "AUS": "AU", "NZL": "NZ", "SGP": "SG", "JPN": "JP",
    "KOR": "KR", "BRA": "BR", "CHL": "CL",
}


def parse_copyright(line: str) -> tuple[str, str]:
    """Split a copyright line into (label_name, parent_text).

    "(P) 2008 RCA Records, a division of Sony Music Entertainment"
        -> ("RCA Records", "Sony Music Entertainment")

    The parent text is what lets the SS9 exclusion gate reject a major-owned imprint
    even when the imprint name itself looks independent.
    """
    if not line:
        return "", ""
    text = _LEAD.sub("", str(line).strip()).strip()
    parts = _PARENT_SPLITS.split(text, maxsplit=1)
    label = parts[0].strip(" .,;/-")
    parent = parts[1].strip(" .,;/-") if len(parts) > 1 else ""
    return label, parent


def _to_signal(item: dict, country: str) -> RawSignal | None:
    label, parent = parse_copyright(item.get("copyright", ""))
    if not label:
        return None
    released = (item.get("releaseDate") or "")[:10]
    artist = item.get("artistName", "")
    collection_id = item.get("collectionId", "")
    return RawSignal(
        raw_label=label,
        signal_type="release_activity",
        description=f"Album '{item.get('collectionName', '')}' by {artist} ({released})"
                    + (f" | copyright parent: {parent}" if parent else ""),
        evidence_url=item.get("collectionViewUrl", "") or f"{LOOKUP}?id={collection_id}",
        observed_at=released,
        source=PROVIDER,
        external_id=f"album:{collection_id}",
        artists=(artist,) if artist else (),
        country_hint=_ISO3_TO_2.get(item.get("country", ""), country),
    )


def search_albums(http: HttpClient, term: str, country: str, *,
                  limit: int = 50, lookback_days: int | None = 365) -> list[RawSignal]:
    """Album search scoped to a country, optionally filtered to recent releases."""
    resp = http.get(SEARCH, provider=PROVIDER,
                    params={"term": term, "entity": "album",
                            "country": country.lower(), "limit": min(limit, 200)},
                    context=f"search:{term}:{country}")
    data = resp.json() or {}
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat() if lookback_days else ""

    signals = []
    for item in data.get("results", []):
        sig = _to_signal(item, country.upper())
        if sig and (not cutoff or (sig.observed_at and sig.observed_at >= cutoff)):
            signals.append(sig)
    return signals


def confirm_label(http: HttpClient, label_name: str, artists: list[str],
                  country: str = "us", per_artist: int = 12) -> dict:
    """Independently corroborate a label by looking up ITS ARTISTS' albums.

    iTunes' search does fuzzy text matching on album/artist names, so searching for
    a label name returns junk ("Earache Records" -> "I Break Records"). Searching a
    known artist and reading the copyright line works reliably:
        Nocturnus -> "(P) 1992 Earache Records Ltd"

    Artists come from MusicBrainz, so this is a genuinely independent second source
    for SS41 B3 cross-source confirmation (worth 5 points) at zero cost.
    """
    target = label_name.casefold().strip()
    matches, matched_artists, latest = [], [], ""
    cutoff = (date.today() - timedelta(days=365)).isoformat()

    for artist in artists[:6]:              # cap the fan-out; these are free but rate-limited
        resp = http.get(SEARCH, provider=PROVIDER,
                        params={"term": artist, "entity": "album",
                                "country": country.lower(), "limit": per_artist},
                        context=f"confirm:{label_name}:{artist}")
        for item in (resp.json() or {}).get("results", []):
            found, parent = parse_copyright(item.get("copyright", ""))
            if not found:
                continue
            f = found.casefold()
            # Bidirectional containment handles "Domino" vs "Domino Recording Co Ltd".
            if target and (target in f or f in target):
                matches.append(item)
                matched_artists.append(item.get("artistName", ""))
                latest = max(latest, (item.get("releaseDate") or "")[:10])

    recent = [m for m in matches if (m.get("releaseDate") or "")[:10] >= cutoff]
    return {
        "confirmed": bool(matches),
        "release_count": len(matches),
        "recent_count": len(recent),
        "latest_release": latest,
        "artists": sorted({a for a in matched_artists if a}),
        "evidence_url": matches[0].get("collectionViewUrl", "") if matches else "",
    }
