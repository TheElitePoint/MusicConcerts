"""Spotify Web API -- the SS12 activity-first discovery workflow.

SS12 describes exactly this loop:
    1. Find an active artist.  2. Review recent releases.  3. Identify the label.
    4. Search the label externally.  5. Determine whether several artists connect to it.

The album object's `label` field makes step 3 a single call, and `/v1/albums?ids=`
batches 20 albums per request, so a country's whole new-release feed costs ~4 calls.

Auth is the client-credentials flow: server-to-server, no user login, no redirect.
Tokens last an hour and are cached in-process.

Spotify restricted several browse endpoints for newly-created apps, so
`new_releases()` falls back to Search with `tag:new` when browse is unavailable.
Both paths return the same album objects with the same `label` field.
"""
from __future__ import annotations

import base64
import time
from datetime import date, timedelta

from .. import config
from ..http import HttpClient
from ..models import RawSignal

ACCOUNTS = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
PROVIDER = "spotify"

# Token cache: (access_token, expires_at_monotonic)
_TOKEN: tuple[str, float] = ("", 0.0)


def available(http: HttpClient) -> bool:
    return config.has_key(PROVIDER) and http.ledger.is_available(PROVIDER)


def _access_token(http: HttpClient) -> str:
    """Client-credentials bearer token, cached until shortly before expiry."""
    global _TOKEN
    token, expires_at = _TOKEN
    if token and time.monotonic() < expires_at:
        return token

    cid, secret = config.env("SPOTIFY_CLIENT_ID"), config.env("SPOTIFY_CLIENT_SECRET")
    if not (cid and secret):
        return ""
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()

    # Never cached to disk: a bearer token is a credential with a 1-hour life.
    import requests
    try:
        resp = requests.post(ACCOUNTS, data={"grant_type": "client_credentials"},
                             headers={"Authorization": f"Basic {basic}"}, timeout=30)
    except requests.RequestException:
        return ""
    http.ledger.record(PROVIDER, "/api/token", status=resp.status_code, context="auth")
    if not resp.ok:
        return ""
    data = resp.json()
    token = data.get("access_token", "")
    _TOKEN = (token, time.monotonic() + int(data.get("expires_in", 3600)) - 60)
    return token


def _auth_headers(http: HttpClient) -> dict:
    token = _access_token(http)
    return {"Authorization": f"Bearer {token}"} if token else {}


def _albums(http: HttpClient, ids: list[str], market: str) -> list[dict]:
    """Fetch full album objects (which carry `label`) 20 at a time."""
    headers = _auth_headers(http)
    if not headers or not ids:
        return []
    out: list[dict] = []
    for i in range(0, len(ids), 20):
        batch = ids[i:i + 20]
        resp = http.get(f"{API}/albums", provider=PROVIDER,
                        params={"ids": ",".join(batch), "market": market},
                        headers=headers, context=f"albums:{market}:{i}")
        if not resp.ok:
            break
        out.extend(a for a in (resp.json() or {}).get("albums", []) if a)
    return out


def _to_signal(album: dict, country: str) -> RawSignal | None:
    label = (album.get("label") or "").strip()
    if not label:
        return None
    artists = tuple(a.get("name", "") for a in album.get("artists", []) if a.get("name"))
    released = (album.get("release_date") or "")[:10]
    return RawSignal(
        raw_label=label,
        signal_type="release_activity",
        description=f"{album.get('album_type', 'album')} '{album.get('name', '')}'"
                    + (f" by {artists[0]}" if artists else "") + f" ({released})",
        evidence_url=(album.get("external_urls") or {}).get(
            "spotify", f"https://open.spotify.com/album/{album.get('id', '')}"),
        observed_at=released,
        source=PROVIDER,
        external_id=f"album:{album.get('id', '')}",
        artists=artists,
        country_hint=country.upper(),
    )


def new_releases(http: HttpClient, country: str, *, limit: int = 50) -> list[RawSignal]:
    """Recent releases in a market, resolved to their labels (SS12)."""
    if not available(http):
        return []
    headers = _auth_headers(http)
    if not headers:
        return []

    resp = http.get(f"{API}/browse/new-releases", provider=PROVIDER,
                    params={"country": country.upper(), "limit": min(limit, 50)},
                    headers=headers, context=f"new-releases:{country}")

    ids: list[str] = []
    if resp.ok:
        ids = [a["id"] for a in ((resp.json() or {}).get("albums") or {}).get("items", [])
               if a.get("id")]

    # Browse is restricted for some newer apps; Search returns equivalent albums.
    if not ids:
        ids = _search_new_album_ids(http, country, limit)

    return [s for s in (_to_signal(a, country) for a in _albums(http, ids, country.upper()))
            if s]


def _search_new_album_ids(http: HttpClient, country: str, limit: int) -> list[str]:
    """Fallback discovery via Search when /browse/new-releases is unavailable."""
    headers = _auth_headers(http)
    if not headers:
        return []
    year = date.today().year
    ids: list[str] = []
    for query in (f"tag:new year:{year}", f"year:{year}"):
        resp = http.get(f"{API}/search", provider=PROVIDER,
                        params={"q": query, "type": "album",
                                "market": country.upper(), "limit": min(limit, 50)},
                        headers=headers, context=f"search-new:{country}")
        if not resp.ok:
            continue
        items = ((resp.json() or {}).get("albums") or {}).get("items", [])
        ids.extend(a["id"] for a in items if a.get("id"))
        if ids:
            break
    return ids


def label_albums(http: HttpClient, label: str, *, country: str = "GB",
                 limit: int = 50, recent_days: int = 365) -> dict:
    """Cross-source confirmation that a label is currently releasing (SS41 B3)."""
    if not available(http):
        return {}
    headers = _auth_headers(http)
    if not headers:
        return {}

    resp = http.get(f"{API}/search", provider=PROVIDER,
                    params={"q": f'label:"{label}"', "type": "album",
                            "market": country.upper(), "limit": min(limit, 50)},
                    headers=headers, context=f"label:{label}")
    if not resp.ok:
        return {}

    items = ((resp.json() or {}).get("albums") or {}).get("items", [])
    cutoff = (date.today() - timedelta(days=recent_days)).isoformat()
    artists: set[str] = set()
    recent = 0
    latest = ""
    for album in items:
        released = (album.get("release_date") or "")[:10]
        latest = max(latest, released)
        if released >= cutoff:
            recent += 1
        for a in album.get("artists", []):
            if a.get("name"):
                artists.add(a["name"])

    return {"confirmed": bool(items), "release_count": len(items),
            "recent_count": recent, "latest_release": latest,
            "artists": sorted(artists),
            "evidence_url": f"https://open.spotify.com/search/{label}/albums"}
