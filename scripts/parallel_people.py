"""Run people discovery with ONE actor over a slice of companies.

Launched several times in parallel (different actor + different company slice), so
the per-developer free allowances are consumed simultaneously instead of serially.
Each process is independent: SQLite is in WAL mode with a 60s busy timeout, and
every stage is idempotent, so concurrent writers are safe.

Usage: parallel_people.py <actor-key> <slice-index> <slice-count>
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config                                    # noqa: E402
from mp.db import utcnow                                 # noqa: E402
from mp.normalize import normalize_company_name, normalize_person_name  # noqa: E402
from mp.pipeline import Context                          # noqa: E402
from mp.sources import apify                             # noqa: E402
from mp.sop.roles import classify_title                  # noqa: E402
from mp.stages.s5_people import people_cap               # noqa: E402

ACTORS = {
    "candidate":  apify.ACTOR_PEOPLE_BY_NAME,             # company NAME, $0.002/result
    "harvest":    apify.ACTOR_PEOPLE_SEARCH_FALLBACK,     # company URL, 10 runs/account
    "neural":     apify.ACTOR_PEOPLE_SEARCH,              # company URL, 1 run/month
    "people":     apify.ACTOR_PEOPLE_BY_NAME_ALT,         # company NAME, own allowance
}
COND = {"company_passed_all_gates": True, "person_has_verified_name": True,
        "person_title_in_role_family": True, "no_cached_profile": True,
        "free_sources_exhausted": True}


# Name-based actors match on the company NAME, so a short or generic name pulls in
# staff from an unrelated business that happens to share it -- "Jo & Co" returned a
# gifting company's employees, "Civilians" a film company's. Those searches are
# skipped unless a LinkedIn company URL is available to disambiguate.
_GENERIC_NAME_TOKENS = {"co", "the", "and", "group", "music", "records", "sound",
                        "media", "audio", "studio", "collective", "productions"}


def name_is_distinctive(company: str) -> bool:
    """Whether a company name is specific enough for a name-only people search.

    A multi-word name is distinctive ("Partisan Records", "The Flenser") -- the
    pairing is what makes it unique. A SINGLE word is not: "Rhino" matched Warner's
    catalogue label and "Civilians" a film company. "Jo & Co" is two words but both
    are two-letter fragments, so it collides just as badly.

    Single-word companies are still reachable via the URL-based actors; only the
    name-only search is skipped for them.
    """
    tokens = [t for t in company.lower().replace("&", " ").replace("-", " ").split()
              if len(t) >= 2]
    if len(tokens) < 2:
        return False
    # At least one token must carry real signal, not just "jo"/"co" fragments.
    return any(len(t) >= 4 for t in tokens)


def payload_for(actor: str, company: str, url: str, cap: int):
    if actor == apify.ACTOR_PEOPLE_BY_NAME:
        return {"currentCompanies": [company], "maxResults": max(cap * 3, 15)}
    if actor == apify.ACTOR_PEOPLE_BY_NAME_ALT:
        return {"currentCompany": company, "maxResults": max(cap * 3, 15)}
    if actor == apify.ACTOR_PEOPLE_SEARCH:
        return {"companies": [url], "maxResults": max(cap * 3, 15)} if url else None
    return ({"profileScraperMode": "Short", "maxItems": max(cap * 3, 15),
             "takePages": 1, "currentCompanies": [url]} if url else None)


def parse(actor: str, items: list[dict], company: str):
    if actor == apify.ACTOR_PEOPLE_BY_NAME:
        return apify._hits_from_candidate_finder(items, company)
    if actor == apify.ACTOR_PEOPLE_BY_NAME_ALT:
        return apify._hits_from_people_search(items, company)
    if actor == apify.ACTOR_PEOPLE_SEARCH:
        return apify._hits_from_neuralverge(items, company)
    return apify._hits_from_harvestapi(items, company)


def main(actor_key: str, index: int, total: int) -> int:
    actor = ACTORS[actor_key]
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        # Only companies never mined before: re-running a worked company returns the
        # same people and burns budget on INSERT OR IGNORE duplicates.
        rows = ctx.db.q("""SELECT company_id, canonical_name, linkedin_url,
                                  roster_estimate
                           FROM companies c WHERE c.status='QUALIFIED'
                             AND NOT EXISTS (SELECT 1 FROM people p
                                             WHERE p.company_id = c.company_id)
                           ORDER BY c.roster_estimate DESC, c.company_id""")
        mine = [r for i, r in enumerate(rows) if i % total == index]
        print(f"[{actor_key}] slice {index}/{total}: {len(mine)} companies", flush=True)

        stored = calls = 0
        for row in mine:
            cap = people_cap(row["roster_estimate"])
            name_based = actor in (apify.ACTOR_PEOPLE_BY_NAME,
                                   apify.ACTOR_PEOPLE_BY_NAME_ALT)
            if name_based and not name_is_distinctive(row["canonical_name"]):
                continue
            body = payload_for(actor, row["canonical_name"], row["linkedin_url"] or "", cap)
            if body is None:
                continue
            try:
                items = apify._run_actor(ctx.http, actor, body,
                                         context=f"{actor_key}:{row['canonical_name']}")
            except Exception:
                continue
            calls += 1
            if not items:
                continue
            expected = normalize_company_name(row["canonical_name"])
            small = (row["roster_estimate"] or 99) <= 20
            for hit in parse(actor, items, row["canonical_name"]):
                if not hit.full_name or not hit.company:
                    continue
                found = normalize_company_name(hit.company)
                if expected and found and not (expected in found or found in expected):
                    continue                      # SS34 employer mismatch
                match = classify_title(hit.title or "", small_company=small)
                if not match.qualifies:
                    continue
                ctx.db.x("""INSERT OR IGNORE INTO people
                            (company_id, full_name, name_normalized, title, title_family,
                             seniority, role_relevance, status, first_seen)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                         (row["company_id"], hit.full_name,
                          normalize_person_name(hit.full_name), hit.title,
                          match.family, match.seniority, match.relevance,
                          "CANDIDATE", utcnow()))
                person = ctx.db.one("""SELECT person_id FROM people
                                       WHERE company_id=? AND name_normalized=?""",
                                    (row["company_id"],
                                     normalize_person_name(hit.full_name)))
                if not person:
                    continue
                pid = person["person_id"]
                if hit.linkedin_url:
                    ctx.db.x("""INSERT OR IGNORE INTO profiles
                                (person_id, channel, url, handle, verification_status,
                                 retrieved_at) VALUES (?,?,?,?,?,?)""",
                             (pid, "linkedin", hit.linkedin_url,
                              hit.linkedin_url.rstrip("/").rsplit("/", 1)[-1],
                              "UNVERIFIED", utcnow()))
                # The actor reports the person's OWN current employer, so this is
                # first-party evidence under SS38 -- exactly what SS42 C3 needs.
                ctx.db.x("""INSERT INTO person_evidence
                            (person_id, kind, url, source, snippet, strength, retrieved_at)
                            VALUES (?,?,?,?,?,?,?)
                            ON CONFLICT(person_id, kind, url) DO UPDATE SET
                              source=excluded.source,
                              snippet=excluded.snippet,
                              strength=excluded.strength,
                              retrieved_at=excluded.retrieved_at""",
                         (pid, "employment", hit.linkedin_url or "",
                          "apify_linkedin",
                          f"LinkedIn current position: {hit.title} at {hit.company}",
                          "strongest", utcnow()))
                stored += 1
        print(f"[{actor_key}] done: {calls} actor calls, {stored} people stored",
              flush=True)
        print(f"[{actor_key}] {ctx.ledger.summary().get('apify')}", flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))
