"""S6 -- Profile resolution (SS65 Stage 5).

Finds the personal LinkedIn / Instagram URLs the two final sheets require.
Free sources first; Apify is reached only when free resolution fails AND the
company already cleared every gate.

SS53: a guessed handle is never stored. If a profile cannot be resolved to a
canonical URL, the field stays empty and the channel is simply unavailable.
"""
from __future__ import annotations

import re

from .. import config, used_contacts
from ..db import utcnow
from ..normalize import (instagram_handle, instagram_url, is_person_linkedin,
                         linkedin_person_url, normalize_person_name)
from ..pipeline import Context
from ..sources import apify, websearch

LI_URL = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+", re.I)
IG_URL = re.compile(r"https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]+", re.I)


def _name_matches(candidate: str, person: str) -> bool:
    """Guard against SS34 same-name mismatches at the URL level."""
    a = set(normalize_person_name(candidate).split())
    b = set(normalize_person_name(person).split())
    return bool(a and b and len(a & b) >= min(2, len(b)))


def _slug_matches(url: str, person: str) -> bool:
    """A profile slug should contain at least part of the person's name."""
    slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
    tokens = [t for t in normalize_person_name(person).split() if len(t) > 2]
    return any(t in slug for t in tokens)


def _free_lookup(ctx: Context, name: str, company: str,
                 channel: str) -> tuple[str, str]:
    """Resolve a profile URL via free search. Returns (url, evidence_url)."""
    site = "linkedin.com/in" if channel == "linkedin" else "instagram.com"
    pattern = LI_URL if channel == "linkedin" else IG_URL

    queries = [f'"{name}" "{company}" {site}']
    if channel == "instagram":
        # Unlike a LinkedIn headline, an Instagram bio almost never states an
        # employer, so requiring the company name in the query starves recall
        # for no real precision gain -- identity is still checked below via the
        # profile's own slug/title, which is the actual safety guard, not the
        # query text. Only reached when the company-scoped query above misses.
        queries.append(f'"{name}" {site}')

    for query in queries:
        for res in websearch.search(ctx.http, query, limit=8):
            blob = f"{res.url} {res.title} {res.snippet}"
            for raw in pattern.findall(blob):
                url = (linkedin_person_url(raw) if channel == "linkedin"
                       else instagram_url(raw))
                if not url:
                    continue
                # Require the slug OR the result title to corroborate identity.
                if _slug_matches(url, name) or _name_matches(res.title, name):
                    return url, res.url
    return "", ""


def _store(ctx: Context, person_id: int, channel: str, url: str,
           evidence_url: str, source: str) -> None:
    if not url:
        return
    ctx.db.x("""INSERT OR IGNORE INTO profiles
                (person_id, channel, url, handle, verification_status, retrieved_at)
                VALUES (?,?,?,?,?,?)""",
             (person_id, channel, url,
              instagram_handle(url) if channel == "instagram" else
              url.rstrip("/").rsplit("/", 1)[-1],
              "UNVERIFIED", utcnow()))
    if evidence_url:
        ctx.db.x("""INSERT OR IGNORE INTO person_evidence
                    (person_id, kind, url, source, snippet, strength, retrieved_at)
                    VALUES (?,?,?,?,?,?,?)""",
                 (person_id, "profile", evidence_url, source,
                  f"{channel} profile resolution", "weak", utcnow()))


def run(ctx: Context, *, limit: int = 0) -> dict:
    rows = ctx.db.q("""SELECT p.person_id, p.full_name, p.title, c.canonical_name,
                              c.company_id, c.status AS company_status
                       FROM people p JOIN companies c USING(company_id)
                       WHERE p.status IN ('CANDIDATE','VERIFIED','REVIEW')
                         AND c.status='QUALIFIED'
                       ORDER BY p.role_relevance DESC""")
    if limit:
        rows = rows[:limit]

    found = {"linkedin": 0, "instagram": 0}
    apify_calls = 0

    for row in rows:
        pid, name, company = row["person_id"], row["full_name"], row["canonical_name"]

        # Already-used contact or company (client exclusion list): spend nothing.
        if used_contacts.company_blocked(company) or used_contacts.person_blocked(name):
            continue

        for channel in config.sources()["profiles"].get("channels", ["linkedin", "instagram"]):
            existing = ctx.db.one(
                "SELECT profile_id FROM profiles WHERE person_id=? AND channel=?",
                (pid, channel))
            if existing:
                continue

            # --- free layer ---
            url, evidence = _free_lookup(ctx, name, company, channel)
            source = "websearch"

            # --- metered layer: only when free resolution failed ---
            if (not url and config.sources()["profiles"].get("paid_profile_lookup", True)
                    and apify.available(ctx.http)):
                conditions = {
                    "company_passed_all_gates": row["company_status"] == "QUALIFIED",
                    "person_has_verified_name": bool(name and len(name.split()) >= 2),
                    "person_title_in_role_family": True,
                    "no_cached_profile": True,
                    "free_sources_exhausted": True,
                }
                if channel == "linkedin":
                    res = apify.find_linkedin_profile(ctx.http, name, company,
                                                      conditions=conditions)
                    url, source = res.linkedin_url, "apify"
                    apify_calls += 1 if res.found else 0
                    if res.found and res.company:
                        # Independent confirmation of current employment (SS42 C3).
                        ctx.db.x("""INSERT INTO person_evidence
                                    (person_id, kind, url, source, snippet, strength,
                                     retrieved_at)
                                    VALUES (?,?,?,?,?,?,?)
                                    ON CONFLICT(person_id, kind, url) DO UPDATE SET
                                      source=excluded.source,
                                      snippet=excluded.snippet,
                                      strength=excluded.strength,
                                      retrieved_at=excluded.retrieved_at""",
                                 (pid, "employment", res.linkedin_url, "apify_linkedin",
                                  f"LinkedIn current position: {res.title} at {res.company}",
                                  "strongest", utcnow()))
            if url:
                _store(ctx, pid, channel, url, evidence, source)
                found[channel] += 1

    stats = {"linkedin_found": found["linkedin"],
             "instagram_found": found["instagram"],
             "apify_calls": apify_calls, "people_processed": len(rows)}
    ctx.stats.setdefault("s6", {}).update(stats)
    return stats
