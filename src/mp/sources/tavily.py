"""Tavily -- the OPPORTUNISTIC second search layer.

Contract, per the project constraints:
  * It runs BESIDE the free layer, only for items free sources could not resolve.
  * It has a hard per-run cap and trips permanently on the first quota error.
  * The pipeline must never depend on it. A run with TAVILY_API_KEY unset must
    produce an identical PASS set for everything the free layer can resolve.

Every function returns an empty result on any failure -- missing key, quota, HTTP
error, tripped breaker -- and never raises into the caller.
"""
from __future__ import annotations

from .. import config
from ..budget import BudgetExceeded
from ..http import HttpClient
from .websearch import AGGREGATORS, SearchResult

ENDPOINT = "https://api.tavily.com/search"
PROVIDER = "tavily"


def available(http: HttpClient) -> bool:
    """Whether Tavily can be consulted at all right now."""
    return config.has_key(PROVIDER) and http.ledger.is_available(PROVIDER)


def search(http: HttpClient, query: str, *, limit: int = 5,
           context: str = "") -> list[SearchResult]:
    """Query Tavily. Returns [] on ANY failure -- never raises, never blocks."""
    if not available(http):
        return []

    resp = None
    for _ in range(len(config.keys(PROVIDER)) or 1):
        api_key = http.ledger.key_for(PROVIDER)
        if not api_key:
            return []
        try:
            # Short timeout, no retries: a plain HTTP error rotates the key ring fine,
            # but a network-level timeout (status 0) never registers as a failure in
            # the budget ledger's consecutive-failure/trip logic (that only counts
            # real HTTP status codes), so it can never trip the breaker on its own.
            # When Tavily's endpoint is hanging rather than erroring, the default
            # 20s timeout x 2 retries (~66s) is paid on EVERY query in the run with
            # no circuit breaker to stop it -- across a full pipeline sweep this has
            # turned a "never depended on" opportunistic layer into a multi-hour
            # stall. Matching the discipline already used for the DDG/SearXNG free
            # backends (timeout=10, retries=0) bounds the worst case per query.
            resp = http.request("POST", ENDPOINT, provider=PROVIDER,
                                json_body={"api_key": api_key,
                                           "query": query,
                                           "max_results": limit,
                                           "search_depth": "basic"},
                                context=context or f"tavily:{query[:60]}",
                                cache_exclude=("api_key",),
                                timeout=8, retries=0)
        except BudgetExceeded:
            return []
        if resp.ok:
            break
        # Quota error rotates the ring; anything else is not key-related.
        if api_key == http.ledger.key_for(PROVIDER):
            return []
    if resp is None or not resp.ok:
        return []

    data = resp.json() or {}
    return [SearchResult(title=r.get("title", ""), url=r.get("url", ""),
                         snippet=r.get("content", ""), backend="tavily")
            for r in data.get("results", []) if r.get("url")]


def find_official_site(http: HttpClient, company: str, extra_terms: str = "") -> str:
    """Tavily-assisted domain resolution. '' when unavailable or inconclusive."""
    results = search(http, f'"{company}" {extra_terms} official website'.strip(),
                     limit=6, context=f"site:{company}")
    tokens = [t for t in company.lower().split() if len(t) > 2]
    for res in results:
        domain = res.domain
        if not domain or any(domain.endswith(a) for a in AGGREGATORS):
            continue
        stem = domain.split(".")[0].replace("-", "")
        if any(t in stem for t in tokens):
            return res.url
    return ""
