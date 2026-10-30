"""S5 -- People discovery at qualified companies (SS30, SS31, SS32).

Runs ONLY for companies that cleared all eight gates (SS29), so no person research
is ever done at a company that cannot produce a final record.

Source order is strictly free-first; Apify is a last resort reached only when free
sources cannot produce enough qualifying candidates:

  1. company /team page      (already fetched in S3 -- costs nothing)
  2. about / contact pages
  3. free web search using the SS30 role families x SS11/SS33 query templates
  4. company Instagram bio
  5. Apify LinkedIn people search                       <- gated, metered

SS51: there is no target headcount. Every person must independently qualify, so a
company yields as many people as genuinely clear the bar -- typically 2-10.
"""
from __future__ import annotations

import re

from .. import config, used_contacts
from ..db import utcnow
from ..normalize import (is_distinctive_company_name, normalize_company_name,
                         normalize_person_name)
from ..pipeline import Context
from ..sop.roles import classify_title
from ..sources import apify, websearch

# Titles worth searching for explicitly, one per SS30 family.
SEARCH_TITLES = [
    "Founder", "CEO", "Managing Director", "Head of Digital", "Head of Marketing",
    "Marketing Director", "Head of A&R", "Label Manager", "Head of Artist Services",
    "Head of Operations", "Head of Campaigns", "Head of Audience Development",
]

# "Jane Smith, Head of Digital at Foo" / "Jane Smith - Head of Digital"
NAME_TITLE = re.compile(
    r"([A-Z][a-z'’\-]+(?:\s+[A-Z][a-z'’\-]+){1,2})\s*(?:,|-|–|—|\|)\s*"
    r"((?:Head of|Director|VP|Vice President|Chief|Founder|Co-Founder|CEO|COO|"
    r"Managing|Senior|Global Head|Label|Artist|Marketing|Digital|A&R|Campaign|"
    r"Partnerships|Commercial|Operations|General)[A-Za-z&,\s]{0,44})")


def people_cap(roster: int | None) -> int:
    """SS51 ceiling for this company, scaled by roster size (4-10).

    A ceiling on how many candidates we retain, never a quota to fill: a company
    with only two genuinely qualifying people still yields two.
    """
    bands = config.sources()["people"].get("people_per_company_by_roster") or []
    for band in bands:
        limit = band.get("max_roster")
        if limit is None or (roster is not None and roster <= int(limit)):
            return int(band["cap"])
    return int(config.sources()["people"].get("max_people_per_company", 10))


def _trim_to_cap(ctx: Context, cid: int, roster: int | None) -> int:
    """Keep only the strongest candidates, up to the SS51 band cap.

    Ranked by SS30 role relevance then seniority, so trimming removes the weakest
    rather than an arbitrary tail.
    """
    cap = people_cap(roster)
    order = {"executive": 0, "senior": 1, "mid_senior": 2, "limited": 3, "junior": 4}
    # SS51: "do not remove genuinely strong people simply because several people
    # already qualify." Anyone who has already reached FINAL PASS is therefore
    # never trimmed -- the cap applies to marginal candidates only.
    rows = ctx.db.q("""SELECT p.person_id, p.role_relevance, p.seniority,
                              EXISTS (SELECT 1 FROM scores s
                                      WHERE s.person_id = p.person_id
                                        AND s.verdict = 'FINAL PASS') AS passed
                       FROM people p
                       WHERE p.company_id=? AND p.status != 'REJECTED'""", (cid,))
    if len(rows) <= cap:
        return 0
    ranked = sorted(rows, key=lambda r: (0 if r["passed"] else 1,
                                         -(r["role_relevance"] or 0),
                                         order.get(r["seniority"] or "", 9)))
    # Never trim a passing record, even if that means exceeding the band cap.
    keep = max(cap, sum(1 for r in rows if r["passed"]))
    ranked_tail = ranked[keep:]
    dropped = 0
    for row in ranked_tail:
        ctx.db.x("""UPDATE people SET status='REJECTED', reject_reason=?
                    WHERE person_id=?""",
                 (f"SS51 band cap: company roster {roster or '?'} allows {cap} people; "
                  "lower-relevance candidate trimmed", row["person_id"]))
        dropped += 1
    return dropped


def _small_company(roster: int | None) -> bool:
    """SS30.2: manager-level titles may qualify at a SMALLER organization."""
    return roster is not None and roster <= 20


def _record(ctx: Context, cid: int, name: str, title: str, source: str,
            evidence_url: str, roster: int | None) -> bool:
    """Store a candidate if it passes SS30/SS31. Returns True when stored."""
    name = " ".join(name.split())
    if not name or len(name.split()) < 2:
        return False
    # Reject section headings that merely look like names (see NON_PERSON_TOKENS).
    from ..sources.website import NON_PERSON_TOKENS
    if any(tok.lower() in NON_PERSON_TOKENS for tok in name.split()):
        return False

    match = classify_title(title, small_company=_small_company(roster))
    if not match.qualifies:
        return False

    ctx.db.x("""INSERT OR IGNORE INTO people
                (company_id, full_name, name_normalized, title, title_family,
                 seniority, role_relevance, status, first_seen)
                VALUES (?,?,?,?,?,?,?,?,?)""",
             (cid, name, normalize_person_name(name), title.strip(),
              match.family, match.seniority, match.relevance, "CANDIDATE", utcnow()))
    row = ctx.db.one("SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
                     (cid, normalize_person_name(name)))
    if row and evidence_url:
        ctx.db.x("""INSERT OR IGNORE INTO person_evidence
                    (person_id, kind, url, source, snippet, strength, retrieved_at)
                    VALUES (?,?,?,?,?,?,?)""",
                 (row["person_id"], "employment", evidence_url, source,
                  f"{name} - {title}",
                  "strongest" if source.startswith("website") else "weak", utcnow()))
    return True


def _classify_existing(ctx: Context, cid: int, roster: int | None) -> int:
    """Classify team members captured during S3 that have no family yet."""
    stored = 0
    for row in ctx.db.q("""SELECT person_id, full_name, title FROM people
                           WHERE company_id=? AND title_family IS NULL""", (cid,)):
        match = classify_title(row["title"] or "", small_company=_small_company(roster))
        if match.qualifies:
            ctx.db.x("""UPDATE people SET title_family=?, seniority=?, role_relevance=?
                        WHERE person_id=?""",
                     (match.family, match.seniority, match.relevance, row["person_id"]))
            stored += 1
        else:
            ctx.db.x("UPDATE people SET status='REJECTED', reject_reason=? WHERE person_id=?",
                     (match.exclusion_reason or "No SS30 role family match",
                      row["person_id"]))
    return stored


# LinkedIn search results follow a very regular shape:
#   "Emily Moxon - Managing Director - Brownswood Recordings | LinkedIn"
#   "Zena White - Partisan Records | LinkedIn"
# Parsing that yields name, title, employer AND profile URL from a single free
# search result -- no metered people-search call needed.
_LI_PERSON_URL = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+", re.I)
# Separators must be SPACE-DELIMITED so an internal hyphen ("Co-Founder") is not
# treated as a field break.
_LI_SPLIT = re.compile(r"\s+[-–—|]\s+|\s*\|\s*")
_LI_TITLE = re.compile(
    r"^\s*([A-Z][\w'’\-\.]+(?:\s+[A-Z][\w'’\-\.]+){1,3})\s+[-–—|]\s+(.+)$")
# "Product Manager at Partisan Records" -> "Product Manager"
_AT_EMPLOYER = re.compile(r"\s+\bat\b\s+.+$", re.I)


# A result block that spans two people ("... UK A&R\n\nJeannette Lee") or whose
# "name" is really a company/title fragment ("King Co", "President Shannon Mann")
# must be discarded: SS50 requires exactly one person per row, and a merged block
# produces a person who does not exist.
_NAME_STOPWORDS = {"co", "ltd", "inc", "llc", "group", "records", "recordings",
                   "music", "president", "founder", "director", "manager", "head",
                   "ceo", "coo", "vp", "chief", "officer", "the"}


def _parse_linkedin_serp(title: str, company: str) -> tuple[str, str]:
    """Extract (name, job_title) from a LinkedIn search-result title. ('','') if unclear."""
    raw = title or ""
    # Multiple people concatenated into one snippet -- unsafe to attribute.
    if "\n" in raw or "\r" in raw:
        return "", ""
    m = _LI_TITLE.match(raw)
    if not m:
        return "", ""
    name, rest = m.group(1).strip(), m.group(2).strip()
    parts = [p.strip() for p in _LI_SPLIT.split(rest) if p.strip()]
    parts = [p for p in parts if p.lower() != "linkedin"]
    if not parts:
        return "", ""

    # The "name" must look like a person, not a company or a role fragment.
    name_tokens = [t.lower().strip(".,") for t in name.split()]
    if any(t in _NAME_STOPWORDS for t in name_tokens):
        return "", ""

    comp_norm = normalize_company_name(company)
    job = ""
    for part in parts:
        candidate = _AT_EMPLOYER.sub("", part).strip()
        if not candidate:
            continue
        # Skip the segment that is just the employer name.
        if comp_norm and normalize_company_name(candidate) == comp_norm:
            continue
        job = candidate
        break
    return name, job


def _search_linkedin_people(ctx: Context, cid: int, company: str,
                            roster: int | None) -> int:
    """Discover decision-makers from LinkedIn search results (SS33), for free.

    Covers the many qualified companies that have no LinkedIn *company* page and so
    cannot be reached by the metered people-search actor.
    """
    found = 0
    seen: set[str] = set()
    for query in (f'"{company}" site:linkedin.com/in',
                  f'"{company}" linkedin head of OR director OR founder'):
        for res in websearch.search(ctx.http, query, limit=10):
            blob = f"{res.url} {res.snippet}"
            urls = _LI_PERSON_URL.findall(blob)
            name, job = _parse_linkedin_serp(res.title, company)
            if not (name and job and urls):
                continue
            url = urls[0].split("?")[0]
            if url in seen:
                continue
            seen.add(url)
            # SS34: the result must actually mention this company.
            if normalize_company_name(company) not in normalize_company_name(
                    f"{res.title} {res.snippet}"):
                continue
            if _record(ctx, cid, name, job, "websearch_linkedin", url, roster):
                found += 1
                person = ctx.db.one(
                    "SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
                    (cid, normalize_person_name(name)))
                if person:
                    ctx.db.x("""INSERT OR IGNORE INTO profiles
                                (person_id, channel, url, handle, verification_status,
                                 retrieved_at) VALUES (?,?,?,?,?,?)""",
                             (person["person_id"], "linkedin", url,
                              url.rstrip("/").rsplit("/", 1)[-1], "UNVERIFIED", utcnow()))
        if found >= people_cap(roster):
            break
    return found


def _search_people(ctx: Context, cid: int, company: str, roster: int | None) -> int:
    """SS33 search logic: company + role, using free search only."""
    found = 0
    for title in SEARCH_TITLES[:6]:
        results = websearch.search(ctx.http, f'"{company}" "{title}"', limit=6)
        for res in results:
            blob = f"{res.title} {res.snippet}"
            # SS34: a quoted-phrase search still returns pages where the company
            # name appears in an unrelated context (especially for short/generic
            # names -- "Legacy", "SO", "Noise Records", "NOTON" all collide with
            # unrelated businesses). Require the same "must actually mention this
            # company" guard used in _search_linkedin_people before extracting a
            # name/title from the snippet, or a plausible name-collision company
            # (a venture firm, a hotel club, an antivirus vendor...) gets recorded
            # as if it were this one.
            if normalize_company_name(company) not in normalize_company_name(blob):
                continue
            for m in NAME_TITLE.finditer(blob):
                if _record(ctx, cid, m.group(1), m.group(2), "websearch",
                           res.url, roster):
                    found += 1
        if found >= people_cap(roster):
            break
    return found


def _apify_people(ctx: Context, row, roster: int | None) -> int:
    """One paid LinkedIn people-search page for a fully gated company.

    Worth the spend because the result carries `currentPosition.companyName`, which
    is an INDEPENDENT confirmation of current employment -- the fact SS42 C3 makes a
    hard gate and which free sources rarely supply on their own.
    """
    cid = row["company_id"]
    company_li = row["linkedin_url"] or ""
    # NOTE: do NOT early-return when company_li is empty. search_people_at_company
    # tries a name-based actor FIRST specifically because it needs no LinkedIn
    # company URL ("it reaches every qualified company rather than the minority
    # with a resolvable company page" -- its own docstring). An early return here
    # was silently skipping Apify for ~99% of companies with no people yet (525/531
    # in one audit), since most qualified companies never get a company LinkedIn
    # URL from free sources. Only the URL-scoped fallback actors inside that
    # function skip themselves when company_li is blank; the free name search does
    # not need it.

    hits = apify.search_people_at_company(
        ctx.http, row["canonical_name"],
        company_linkedin_url=company_li,
        conditions={
            "company_passed_all_gates": row["status"] == "QUALIFIED",
            "person_has_verified_name": True,
            "person_title_in_role_family": True,
            "no_cached_profile": True,
            "free_sources_exhausted": True,
        })

    # SS34/SS52 identity guard: LinkedIn reports each person's OWN current employer,
    # so it is the authoritative check that we searched the right company page. A
    # plausible-looking slug is not enough -- e.g. "Hear No Evil Recordings" (a UK
    # label) resolves to a LinkedIn page belonging to an unrelated recording studio.
    expected = normalize_company_name(row["canonical_name"])
    mismatched = 0

    stored = 0
    for hit in hits:
        if not hit.found:
            continue
        found_company = normalize_company_name(hit.company or "")
        if expected and found_company and not (
                expected in found_company or found_company in expected):
            mismatched += 1
            continue
        # NOTE: must NOT start with "apify_linkedin" -- STRONGEST_EMPLOYMENT_SOURCES
        # in s7_person_verify checks str.startswith(), which matches on prefix.
        source_tag = "serp_employees_scraper" if hit.weak_evidence else "apify_linkedin"
        if not _record(ctx, cid, hit.full_name, hit.title, source_tag,
                       hit.linkedin_url, roster):
            continue
        stored += 1
        person = ctx.db.one(
            "SELECT person_id FROM people WHERE company_id=? AND name_normalized=?",
            (cid, normalize_person_name(hit.full_name)))
        if not person:
            continue
        pid = person["person_id"]
        # Store the resolved profile so S6 need not pay to find it again.
        ctx.db.x("""INSERT OR IGNORE INTO profiles
                    (person_id, channel, url, handle, verification_status, retrieved_at)
                    VALUES (?,?,?,?,?,?)""",
                 (pid, "linkedin", hit.linkedin_url,
                  hit.linkedin_url.rstrip("/").rsplit("/", 1)[-1], "UNVERIFIED", utcnow()))
        # LinkedIn's own current-company claim = a second employment source (SS42 C3).
        if hit.company:
            ctx.db.x("""INSERT INTO person_evidence
                        (person_id, kind, url, source, snippet, strength, retrieved_at)
                        VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(person_id, kind, url) DO UPDATE SET
                          source=excluded.source,
                          snippet=excluded.snippet,
                          strength=excluded.strength,
                          retrieved_at=excluded.retrieved_at""",
                     (pid, "employment", hit.linkedin_url, source_tag,
                      f"LinkedIn current position: {hit.title} at {hit.company}"
                      + (f" (tenure {hit.tenure})" if hit.tenure else ""),
                      "weak" if hit.weak_evidence else "strongest", utcnow()))

    if mismatched and not stored:
        # Every returned person works somewhere else: the LinkedIn company URL is
        # wrong. Clear it so the bad page is not reused, and flag for review.
        ctx.db.x("UPDATE companies SET linkedin_url=NULL WHERE company_id=?", (cid,))
        ctx.bump("company_linkedin_url_rejected")
    return stored


PAID_SEARCH_TYPES = {"record_label", "artist_management", "music_distributor", "label_services"}
SHEET_QUOTA_PER_COMPANY = 6


def _worth_paid_search(ctx: Context, row) -> bool:
    """Spend a paid people search only where a found person could become a row.

    Mirrors the export gates: Platform company type, a multi-artist roster,
    activity inside a year, at least two evidenced artists, and room left under
    the per-company quota. Anything else is credit spent on people who can never
    reach the sheet.
    """
    cid = row["company_id"]
    majors = config.sources()["people"].get("skip_major_imprints", [])
    name = normalize_company_name(row["canonical_name"])
    if any(name == m or name.startswith(m + " ") for m in majors):
        ctx.bump("paid_skip_major_imprint")
        return False
    info = ctx.db.one("SELECT company_type, roster_estimate FROM companies WHERE company_id=?",
                      (cid,))
    if info["company_type"] not in PAID_SEARCH_TYPES or (info["roster_estimate"] or 0) < 2:
        ctx.bump("paid_skip_company_shape")
        return False
    recent = ctx.db.one("""SELECT COUNT(*) n FROM activity_signals WHERE company_id=?
                           AND observed_at >= date('now','-365 days')""", (cid,))["n"]
    artists = ctx.db.one("SELECT COUNT(*) n FROM artists WHERE company_id=?", (cid,))["n"]
    if recent < 1 or artists < 2:
        ctx.bump("paid_skip_no_activity_or_artists")
        return False
    in_sheet = ctx.db.one("""SELECT COUNT(*) n FROM sheet_membership m JOIN people p USING(person_id)
                             WHERE p.company_id=? AND m.channel='linkedin'""", (cid,))["n"]
    if in_sheet >= SHEET_QUOTA_PER_COMPANY:
        ctx.bump("paid_skip_company_full")
        return False
    return True


def run(ctx: Context, *, limit: int = 0) -> dict:
    # Start each sweep from the first account: per-actor caps are re-detected live,
    # and a fresh account may have allowance the previous run never reached.
    ctx.ledger.keyring.ring("apify").index = 0

    # Metered people-search runs are scarce, so spend them where the SS40 A2 roster
    # band scores highest (3-20 artists = 10/10) and activity evidence is richest.
    # Companies without a LinkedIn URL are ordered last: the search cannot work for
    # them, so they must never consume a run.
    rows = ctx.db.q("""SELECT company_id, canonical_name, roster_estimate, country,
                              linkedin_url, status,
                              (SELECT COUNT(*) FROM activity_signals a
                                WHERE a.company_id = companies.company_id) AS sigs
                       FROM companies WHERE status='QUALIFIED'
                       ORDER BY (linkedin_url IS NULL),
                                CASE
                                  WHEN roster_estimate BETWEEN 3 AND 20 THEN 0
                                  WHEN roster_estimate BETWEEN 21 AND 25 THEN 1
                                  WHEN roster_estimate BETWEEN 26 AND 30 THEN 2
                                  ELSE 3 END,
                                sigs DESC, company_id""")
    if limit:
        rows = rows[:limit]

    total_candidates = 0
    apify_used = 0

    for row in rows:
        cid, name, roster = row["company_id"], row["canonical_name"], row["roster_estimate"]

        # Already-used company (client exclusion list): never search it again.
        if used_contacts.company_blocked(name):
            ctx.bump("skipped_used_company")
            continue

        # 1-2. Free: team members already captured from the website in S3.
        found = _classify_existing(ctx, cid, roster)
        # 3. Free: LinkedIn search-result parsing -- name, title, employer and
        #    profile URL from one result, no metered call.
        found += _search_linkedin_people(ctx, cid, name, roster)
        # 4. Free: SS11/SS33 company+role queries.
        found += _search_people(ctx, cid, name, roster)

        qualifying = ctx.db.one("""SELECT COUNT(*) n FROM people
                                   WHERE company_id=? AND status='CANDIDATE'""", (cid,))["n"]

        # 5. Apify -- ONLY when free sources genuinely came up short.
        threshold = config.sources()["people"]["apify_min_free_candidates"]
        # A people-name search for a generic name ("OFF", "CAM", "Jo & Co") returns
        # strangers who share the word; every such result is later rejected by the
        # employer check, so the run is paid for nothing.
        if (qualifying < threshold
                and config.sources()["people"].get("paid_name_search", True)
                and apify.available(ctx.http)
                and is_distinctive_company_name(name)
                and _worth_paid_search(ctx, row)):
            ctx.bump("companies_below_free_threshold")
            before = ctx.ledger.summary().get("apify", {}).get("calls", 0)
            found += _apify_people(ctx, row, roster)
            apify_used += ctx.ledger.summary().get("apify", {}).get("calls", 0) - before

        ctx.bump("trimmed_over_cap", _trim_to_cap(ctx, cid, roster))
        total_candidates += found

    stats = {"candidates": total_candidates,
             "companies_processed": len(rows), "apify_calls": apify_used}
    ctx.stats.setdefault("s5", {}).update(stats)
    return stats
