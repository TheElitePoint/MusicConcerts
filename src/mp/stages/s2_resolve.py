"""S2 -- Company resolution (SS65 Stage 2).

Turns a raw label string from discovery into a canonical company with a real,
verified domain. Fallback order is strictly free-first:

    1. MusicBrainz url-rels   official homepage attached to the label entity
    2. Wikidata               P856 website, P17 country, P749/P127 parent
    3. Discogs                label profile URLs                (when token present)
    4. Free web search        DuckDuckGo -> SearXNG
    5. Tavily                 opportunistic only, never depended on

SS53: when no domain resolves, the company is marked UNRESOLVED and routed to the
review queue. A guessed URL is worse than no URL.

This stage also performs the SS9 re-check, because parent-company ownership is only
knowable AFTER enrichment -- discovery could only see the name.
"""
from __future__ import annotations

from .. import config
from ..db import utcnow
from ..normalize import registrable_domain
from ..pipeline import Context
from ..sop.exclusions import check_excluded_brand
from ..sources import discogs, musicbrainz, tavily, websearch, wikidata


def _resolve_one(ctx: Context, name: str) -> dict:
    """Walk the fallback chain until a domain resolves. Returns a facts dict."""
    order = config.sources()["resolution"]["enabled"]
    facts = {"website": "", "domain": "", "country": "", "parent": "",
             "mbid": "", "discogs_id": None, "resolved_via": "", "evidence": []}

    # --- 1. MusicBrainz: label entity -> official homepage -------------------
    if "musicbrainz" in order:
        candidates = musicbrainz.search_label(ctx.http, name, limit=3)
        # Only trust a high-scoring, near-exact name match.
        for cand in candidates:
            if cand["score"] >= 90 and cand["name"].casefold() == name.casefold():
                facts["mbid"] = cand["mbid"]
                if cand.get("country"):
                    facts["country"] = cand["country"]
                urls = musicbrainz.label_urls(ctx.http, cand["mbid"])
                home = musicbrainz.official_homepage(urls)
                if home:
                    facts.update(website=home, domain=registrable_domain(home),
                                 resolved_via="musicbrainz")
                    facts["evidence"].append(
                        ("identity", f"https://musicbrainz.org/label/{cand['mbid']}",
                         "musicbrainz", "strong"))
                break

    # --- 2. Wikidata: authoritative country + parent org ---------------------
    if "wikidata" in order:
        wd = wikidata.lookup_company(ctx.http, name)
        if wd:
            facts["parent"] = wd.get("parent", "") or facts["parent"]
            facts["country"] = facts["country"] or wd.get("country", "")
            if wd.get("evidence_url"):
                facts["evidence"].append(
                    ("identity", wd["evidence_url"], "wikidata", "strong"))
            if not facts["website"] and wd.get("website"):
                facts.update(website=wd["website"],
                             domain=registrable_domain(wd["website"]),
                             resolved_via="wikidata")

    # --- 3. Discogs label profile -> official site --------------------------
    if "discogs" in order:
        candidates = discogs.search_label(ctx.http, name, limit=3)
        exact = next((c for c in candidates if c["exact"]), None)
        if exact:
            facts["discogs_id"] = exact["id"]
            detail = discogs.label_detail(ctx.http, exact["id"])
            facts["evidence"].append(("identity", exact["url"], "discogs", "strong"))
            site = discogs.official_site(detail)
            if site and not facts["website"]:
                facts.update(website=site, domain=registrable_domain(site),
                             resolved_via="discogs")

    # --- 4. Free web search --------------------------------------------------
    if not facts["website"] and "websearch" in order:
        url, evidence = websearch.find_official_site(ctx.http, name, "record label music")
        if url:
            facts.update(website=url, domain=registrable_domain(url),
                         resolved_via="websearch")
            facts["evidence"].append(("identity", evidence, "websearch", "weak"))

    # --- 5. Tavily: opportunistic last layer, never a dependency -------------
    if not facts["website"] and "tavily" in order:
        url = tavily.find_official_site(ctx.http, name, "music label")
        if url:
            facts.update(website=url, domain=registrable_domain(url),
                         resolved_via="tavily")
            facts["evidence"].append(("identity", url, "tavily", "weak"))
            ctx.bump("tavily_resolved")

    return facts


def run(ctx: Context, *, limit: int = 0) -> dict:
    """Resolve all NEW companies. Returns run stats."""
    rows = ctx.db.q("""SELECT company_id, canonical_name FROM companies
                       WHERE status = 'NEW' ORDER BY company_id""")
    if limit:
        rows = rows[:limit]

    resolved = unresolved = excluded = 0

    for row in rows:
        cid, name = row["company_id"], row["canonical_name"]
        facts = _resolve_one(ctx, name)

        # SS9 re-check: parent ownership is only visible after enrichment.
        brand = check_excluded_brand(name, parent=facts["parent"])
        if brand.excluded:
            ctx.db.x("UPDATE companies SET status='REJECTED', last_researched=? "
                     "WHERE company_id=?", (utcnow(), cid))
            ctx.db.x("""INSERT INTO company_rejections
                        (company_id, gate, reason_code, reason_text, rejected_at, recheck_after)
                        VALUES (?,?,?,?,?,NULL)""",
                     (cid, "G4", "excluded_brand_parent", brand.reason, utcnow()))
            excluded += 1
            ctx.bump("rejected_brand_after_enrichment")
            continue

        for kind, url, source, strength in facts["evidence"]:
            if url:
                ctx.db.x("""INSERT OR IGNORE INTO company_evidence
                            (company_id, kind, url, source, snippet, strength, retrieved_at)
                            VALUES (?,?,?,?,?,?,?)""",
                         (cid, kind, url, source, f"resolution via {source}",
                          strength, utcnow()))

        if facts["website"]:
            # A domain collision means two labels resolved to the same company (SS52).
            existing = ctx.db.one(
                "SELECT company_id FROM companies WHERE domain = ? AND company_id != ?",
                (facts["domain"], cid))
            if existing:
                ctx.db.x("UPDATE companies SET status='REJECTED', last_researched=? "
                         "WHERE company_id=?", (utcnow(), cid))
                ctx.db.x("""INSERT INTO company_rejections
                            (company_id, gate, reason_code, reason_text, rejected_at, recheck_after)
                            VALUES (?,?,?,?,?,NULL)""",
                         (cid, "DEDUPE", "duplicate_domain",
                          f"Duplicate of company_id {existing['company_id']} "
                          f"(same domain {facts['domain']})", utcnow()))
                ctx.bump("deduped")
                continue

            ctx.db.x("""UPDATE companies SET website=?, domain=?, country=COALESCE(?, country),
                        country_confidence=?, parent_org=?, mbid=?, discogs_id=?,
                        status='RESOLVED', last_researched=? WHERE company_id=?""",
                     (facts["website"], facts["domain"], facts["country"] or None,
                      "single_source" if facts["country"] else "unknown",
                      facts["parent"] or None, facts["mbid"] or None,
                      facts["discogs_id"], utcnow(), cid))
            resolved += 1
        else:
            # SS53: never invent a URL. Route to review instead.
            ctx.db.x("UPDATE companies SET status='UNRESOLVED', parent_org=?, mbid=?, "
                     "discogs_id=?, last_researched=? WHERE company_id=?",
                     (facts["parent"] or None, facts["mbid"] or None,
                      facts["discogs_id"], utcnow(), cid))
            unresolved += 1
            ctx.bump("unresolved_no_domain")

    stats = {"resolved": resolved, "unresolved": unresolved,
             "excluded_after_enrichment": excluded}
    ctx.stats.setdefault("s2", {}).update(stats)
    return stats
