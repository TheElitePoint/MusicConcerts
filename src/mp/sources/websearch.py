"""Free web search -- the substitute for SS11's Google-based workflows.

Google has no free API, so the dependency is free scraping: DuckDuckGo's HTML
endpoint first, then SearXNG public instances. Results are cached for 14 days, and
a per-run query cap in budget.yaml stops a runaway crawl.

Tavily (sources/tavily.py) sits BESIDE this, not in front of it: it is consulted
only for items the free layer failed to resolve, and it never blocks the pipeline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from .. import config
from ..http import HttpClient
from ..normalize import registrable_domain

PROVIDER = "websearch"
DDG = "https://html.duckduckgo.com/html/"

# Scraped search backends get throttled or blocked without warning, and a dead one
# costs a full retry cycle (~75s) on EVERY query. After this many consecutive
# empty/failed responses a backend is skipped for the rest of the run.
_BACKEND_FAILURE_LIMIT = 3
_dead_backends: set[str] = set()
_failure_counts: dict[str, int] = {}


def _note_backend_result(backend: str, ok: bool) -> None:
    if ok:
        _failure_counts[backend] = 0
        return
    _failure_counts[backend] = _failure_counts.get(backend, 0) + 1
    if _failure_counts[backend] >= _BACKEND_FAILURE_LIMIT:
        _dead_backends.add(backend)


def backend_status() -> dict:
    """Which scraped backends are still usable this run."""
    return {"dead": sorted(_dead_backends), "failures": dict(_failure_counts)}


def reset_backends() -> None:
    _dead_backends.clear()
    _failure_counts.clear()

# Hosts that are never a company's own website.
AGGREGATORS = {
    "wikipedia.org", "youtube.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "linkedin.com", "discogs.com", "musicbrainz.org", "last.fm",
    "spotify.com", "apple.com", "deezer.com", "soundcloud.com", "bandcamp.com",
    "amazon.com", "tiktok.com", "crunchbase.com", "bloomberg.com", "pinterest.com",
    "reddit.com", "genius.com", "allmusic.com", "rateyourmusic.com", "songkick.com",
    "bandsintown.com", "ticketmaster.com", "eventbrite.com", "medium.com",
    "wordpress.com", "blogspot.com", "glassdoor.com", "indeed.com", "zoominfo.com",
}


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    backend: str = ""

    @property
    def domain(self) -> str:
        return registrable_domain(self.url)


def _unwrap_ddg(href: str) -> str:
    """DuckDuckGo wraps results as //duckduckgo.com/l/?uddg=<encoded-target>."""
    if not href:
        return ""
    if "uddg=" in href:
        qs = parse_qs(urlparse("https:" + href if href.startswith("//") else href).query)
        if qs.get("uddg"):
            return unquote(qs["uddg"][0])
    return "https:" + href if href.startswith("//") else href


def _search_ddg(http: HttpClient, query: str, limit: int) -> list[SearchResult]:
    resp = http.get(DDG, provider=PROVIDER, params={"q": query},
                    headers={"Accept-Language": "en-US,en;q=0.9"},
                    context=f"ddg:{query[:60]}", timeout=10, retries=0)
    if not resp.ok:
        return []
    soup = BeautifulSoup(resp.text, "lxml")
    out: list[SearchResult] = []
    for node in soup.select("div.result, div.web-result")[:limit * 2]:
        link = node.select_one("a.result__a")
        if not link:
            continue
        url = _unwrap_ddg(link.get("href", ""))
        if not url:
            continue
        snippet_node = node.select_one(".result__snippet")
        out.append(SearchResult(title=link.get_text(strip=True), url=url,
                                snippet=snippet_node.get_text(" ", strip=True)
                                if snippet_node else "", backend="duckduckgo"))
        if len(out) >= limit:
            break
    return out


def _search_searxng(http: HttpClient, query: str, limit: int) -> list[SearchResult]:
    """HTML-parsing fallback. Public instances rotate and rate-limit; failures fall
    through silently to the next instance."""
    for instance in config.sources()["websearch"]["searxng"]["instances"]:
        resp = http.get(instance, provider=PROVIDER, params={"q": query},
                        headers={"Accept-Language": "en-US,en;q=0.9"},
                        context=f"searx:{query[:60]}", timeout=10, retries=0)
        if not resp.ok:
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        out: list[SearchResult] = []
        for art in soup.select("article.result, div.result")[:limit * 2]:
            link = art.select_one("a.url_wrapper, h3 a, a[href^=http]")
            if not link:
                continue
            url = link.get("href", "")
            if not url.startswith("http"):
                continue
            body = art.select_one("p.content, .content")
            out.append(SearchResult(title=link.get_text(strip=True), url=url,
                                    snippet=body.get_text(" ", strip=True) if body else "",
                                    backend="searxng"))
            if len(out) >= limit:
                break
        if out:
            return out
    return []


def search(http: HttpClient, query: str, *, limit: int = 10) -> list[SearchResult]:
    """Run a query through the free backends, then Tavily. [] if all fail.

    Tavily sits BEHIND the scraped backends, not in front: free sources are tried
    first and Tavily is consulted only for what they could not answer. That keeps
    the original contract (never depend on Tavily) while making it genuinely useful
    when DuckDuckGo and SearXNG are throttled -- which is their normal state after
    a few hundred queries.
    """
    for backend in config.sources()["websearch"]["backends"]:
        if backend in _dead_backends:
            continue
        fn = {"duckduckgo": _search_ddg, "searxng": _search_searxng}.get(backend)
        if not fn:
            continue
        try:
            results = fn(http, query, limit)
        except Exception:
            # A scraping backend breaking must never take the pipeline down.
            results = []
        _note_backend_result(backend, bool(results))
        if results:
            return results

    # Opportunistic secondary layer. Import here to avoid a circular import.
    from . import tavily
    return tavily.search(http, query, limit=limit, context=f"fallback:{query[:60]}")


def find_official_site(http: HttpClient, company: str,
                       extra_terms: str = "") -> tuple[str, str]:
    """Best-guess official domain for a company. Returns (url, evidence_url).

    Returns ("", "") rather than a guess when nothing scores well enough -- SS53
    forbids submitting an unverified URL, so an empty result is the correct answer.
    """
    query = f'"{company}" {extra_terms}'.strip()
    results = search(http, query, limit=10)
    if not results:
        return "", ""

    tokens = [t for t in re.split(r"\W+", company.lower()) if len(t) > 2]
    best, best_score = None, 0

    for rank, res in enumerate(results):
        domain = res.domain
        if not domain or any(domain.endswith(a) for a in AGGREGATORS):
            continue
        score = max(0, 10 - rank)                      # earlier results rank higher
        stem = domain.split(".")[0].replace("-", "")
        # Company tokens appearing in the domain is the strongest signal.
        matched = sum(1 for t in tokens if t in stem)
        score += matched * 12
        if matched == len(tokens) and tokens:
            score += 15
        if company.lower() in res.title.lower():
            score += 6
        if score > best_score:
            best, best_score = res, score

    # Threshold: require real name evidence in the domain, not just a top ranking.
    if best is None or best_score < 20:
        return "", ""
    return best.url, best.url
