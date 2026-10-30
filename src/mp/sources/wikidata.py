"""Wikidata -- keyless structured facts about music companies.

Uniquely valuable for two SOP requirements that are otherwise expensive:
  * SS7 country determination (P17 -> P297 ISO-2) -- a structured, citable country
    claim, used as one of the two agreeing signals the SOP demands.
  * SS9 parent-organization detection (P749 parent / P127 owned-by) -- surfaces that
    an independent-looking imprint sits inside an excluded core brand.

Transport: the SPARQL endpoint is primary because the MediaWiki entity API rate-limits
shared IPs aggressively. Both paths degrade to {} on failure -- absence from Wikidata
is common and is NEVER a rejection reason. This is enrichment, not a gate.
"""
from __future__ import annotations

from ..http import HttpClient

SPARQL = "https://query.wikidata.org/sparql"
API = "https://www.wikidata.org/w/api.php"
PROVIDER = "wikidata"

# Exact English-label match. Fast, index-backed, and precise enough that we avoid
# the album/song entities that fuzzy search would return for a label name.
_Q_EXACT = '''SELECT ?item ?itemLabel ?desc ?site ?iso ?parentLabel ?inception WHERE {
  ?item rdfs:label "%s"@en .
  OPTIONAL { ?item wdt:P856 ?site . }
  OPTIONAL { ?item wdt:P17 ?c . ?c wdt:P297 ?iso . }
  OPTIONAL { ?item wdt:P749 ?p . ?p rdfs:label ?parentLabel . FILTER(LANG(?parentLabel)="en") }
  OPTIONAL { ?item wdt:P127 ?o . ?o rdfs:label ?parentLabel . FILTER(LANG(?parentLabel)="en") }
  OPTIONAL { ?item wdt:P571 ?inception . }
  OPTIONAL { ?item schema:description ?desc . FILTER(LANG(?desc)="en") }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
} LIMIT 5'''

# Descriptions that mark a genuine music company rather than a same-named album.
_MUSIC_HINTS = ("record label", "record company", "music label", "music company",
                "recording company", "management company", "music publisher",
                "music distributor", "independent label")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _sparql(http: HttpClient, query: str, context: str) -> list[dict]:
    """Run a SPARQL query. Returns [] on any failure -- never raises."""
    resp = http.get(SPARQL, provider=PROVIDER,
                    params={"query": query, "format": "json"},
                    headers={"Accept": "application/sparql-results+json"},
                    context=context)
    if not resp.ok:
        return []
    data = resp.json() or {}
    return [{k: v.get("value", "") for k, v in b.items()}
            for b in (data.get("results") or {}).get("bindings", [])]


def lookup_company(http: HttpClient, name: str) -> dict:
    """Structured facts for a company by name. Returns {} when not found.

    Never raises and never blocks the pipeline: a Wikidata outage simply means the
    country claim must come from another source (SS7 requires two agreeing signals,
    and the company website is always the stronger one).
    """
    if not name:
        return {}
    rows = _sparql(http, _Q_EXACT % _escape(name), f"lookup:{name}")
    if not rows:
        return {}

    # Prefer a row that both looks like a music company and carries a website.
    def rank(row: dict) -> tuple[int, int]:
        desc = (row.get("desc") or "").lower()
        return (1 if any(h in desc for h in _MUSIC_HINTS) else 0,
                1 if row.get("site") else 0)

    row = max(rows, key=rank)
    qid = row.get("item", "").rsplit("/", 1)[-1]
    return {
        "qid": qid,
        "name": row.get("itemLabel", name),
        "description": row.get("desc", ""),
        "website": row.get("site", ""),
        "country": (row.get("iso") or "").upper(),   # ISO-2, feeds SS7
        "parent": row.get("parentLabel", ""),        # feeds SS9 exclusion gate
        "inception": (row.get("inception") or "")[:10],
        "evidence_url": f"https://www.wikidata.org/wiki/{qid}" if qid else "",
    }


def labels_in_country(http: HttpClient, iso2: str, limit: int = 200) -> list[dict]:
    """Record labels registered to a country, with websites.

    A supplementary discovery seed (SS26): directory-style listings never qualify a
    company on their own, but they surface names worth checking for current activity.
    """
    query = '''SELECT ?item ?itemLabel ?site WHERE {
      ?item wdt:P31/wdt:P279* wd:Q18127 .
      ?item wdt:P17 ?c . ?c wdt:P297 "%s" .
      ?item wdt:P856 ?site .
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
    } LIMIT %d''' % (_escape(iso2.upper()), limit)
    rows = _sparql(http, query, f"labels_in:{iso2}")
    return [{"qid": r.get("item", "").rsplit("/", 1)[-1],
             "name": r.get("itemLabel", ""),
             "website": r.get("site", ""),
             "country": iso2.upper()} for r in rows if r.get("itemLabel")]
