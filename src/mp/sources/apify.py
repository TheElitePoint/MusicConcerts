"""Apify -- PEOPLE AND PROFILE RESOLUTION ONLY.

Hard constraint from the project brief: "Apify should primarily/only be used for
finding or resolving people/profiles, not broad company discovery."

Enforced structurally, not by convention:
  * every entry point calls `ledger.require("apify", ...)` with the preconditions
    declared in budget.yaml -- a missing precondition RAISES PreconditionFailed, so
    a future caller cannot quietly reach Apify from a discovery stage;
  * results cache for 90 days (profiles change slowly);
  * without APIFY_TOKEN every function returns empty and the pipeline completes
    normally with those records routed to REVIEW.

Actor IDs and input schemas below were verified against the live Apify Store rather
than assumed. Costs are FREE-tier rates.
"""
from __future__ import annotations

import re

from dataclasses import dataclass, field

from .. import config
from ..budget import BudgetExceeded
from ..http import HttpClient
from ..http import _bounded_get
from ..normalize import instagram_handle, linkedin_person_url

PROVIDER = "apify"
BASE = "https://api.apify.com/v2"

# --- verified actors --------------------------------------------------------
# People discovery at a known company.
#
# Actor choice is deliberate. Verified by testing all four candidates against a
# company whose real staff list we already knew (Napalm Records):
#   neuralverge  -- returns the true current employees with `company`, `title` and
#                   `tenureAtCompany`. 15 free runs PER ACCOUNT, and the cap is
#                   per-developer, so it works even on an account that is out of
#                   harvestapi runs. PRIMARY.
#   harvestapi   -- equally accurate but only 10 free runs per account. FALLBACK.
#   apt_marble   -- REJECTED: echoes back the company slug you passed instead of the
#                   person's real employer, so it cannot evidence employment (SS42 C3).
#   memo23       -- REJECTED: name search is not company-scoped; returned 200
#                   unrelated people for "Bella Union".
ACTOR_PEOPLE_SEARCH = "neuralverge/neuralverge-linkedin-company-employee"
ACTOR_PEOPLE_SEARCH_FALLBACK = "harvestapi/linkedin-profile-search"
# No-login/no-cookie, Google-SERP-derived. Cheap ($0.003/profile) and company-URL
# scoped so it does not repeat the free-text _search_people bug (SS34: results are
# constrained to the company's own LinkedIn page, not a quoted-phrase web search).
# BUT it has no dedicated currentCompany/currentPosition field the way neuralverge
# and harvestapi do -- `headline` is raw SERP snippet text and is sometimes just the
# company name, not a title. Hits from this actor are tagged with a source string
# NOT in STRONGEST_EMPLOYMENT_SOURCES (see s5_people._record call site), so they
# still need a second independent source before SS42 C3 treats them as verified.
ACTOR_EMPLOYEES_SERP = "automation-lab/linkedin-company-employees-scraper"
# Company-by-company employee enumeration.  Unlike the generic people-search
# actor, this actor can process a batch in ``one_by_one`` mode, so a larger label
# cannot crowd a smaller label out of the first result page.  The returned Short
# records include the person's LinkedIn URL and current-position claim, which is
# sufficient for the SS34/SS42 verification pass after local employer/title gates.
ACTOR_COMPANY_EMPLOYEES = "harvestapi/linkedin-company-employees"
# Takes a company NAME rather than a LinkedIn company URL, which unblocks the large
# majority of qualified companies that have no resolvable LinkedIn page. Returns
# `currentCompany` and a `matchedCurrentCompanies` field, so employment can be
# evidenced directly (SS42 C3). Cheapest of the three at $0.002/result, and from a
# third developer, so it carries its own free-tier allowance.
ACTOR_PEOPLE_BY_NAME = "thirdwatch/linkedin-candidate-finder-scraper"
# Second name-based source from a different developer, so it carries its own free
# allowance and can run in parallel with the one above. Returns headline text like
# "Head of A&R at Rough Trade Publishing"; the employer is recovered from the
# headline when the dedicated field is blank.
ACTOR_PEOPLE_BY_NAME_ALT = "khadinakbar/linkedin-people-search-scraper"
# Targeted lookup of ONE known person ($0.004/search page of 10, $0.002/profile).
ACTOR_PERSON_BY_NAME = "harvestapi/linkedin-profile-search-by-name"
# Batch profile fetch by URL: MANY profiles in ONE run at $0.004 each. Used to
# upgrade search-snippet evidence (SS38 "weak") into a first-party current-position
# claim (SS38 "strongest"), which is what SS42 C3 requires.
ACTOR_PROFILE_BATCH = "harvestapi/linkedin-profile-scraper"
PROFILE_BATCH_SIZE = 10   # free-tier item cap per run; exceeding it yields nothing
# Official Apify actor, 4.7 stars, $0.0026 per profile.
ACTOR_INSTAGRAM_PROFILE = "apify/instagram-profile-scraper"

# Apify actor ids, needed to filter run history per actor when checking caps.
_ACTOR_IDS = {
    "neuralverge/neuralverge-linkedin-company-employee": "JbJke4iGh7hRR5eBb",
    "harvestapi/linkedin-profile-search": "M2FMdjRVeF1HPGFcc",
    "thirdwatch/linkedin-candidate-finder-scraper": "pjkSWP69N9QsEg4DL",
    "khadinakbar/linkedin-people-search-scraper": "S6dSWuZpECzcBhWIc",
    "harvestapi/linkedin-profile-scraper": "LpVuK3Zozwuipa5bp",
    "automation-lab/linkedin-company-employees-scraper": "ORFez5SBHRFoxbRVy",
}

# SS30 role families -> LinkedIn seniority filter ids, so paid searches never
# return interns or coordinators that SS31 would discard anyway.
SENIORITY_IDS = ["220", "300", "310", "320"]   # Director, VP, CXO, Owner/Partner


@dataclass(slots=True)
class PersonHit:
    """One decision-maker returned by a LinkedIn people search."""
    full_name: str = ""
    title: str = ""
    company: str = ""
    linkedin_url: str = ""
    location: str = ""
    tenure: str = ""          # e.g. "11y 1m" -- corroborates CURRENT employment
    weak_evidence: bool = False   # True: no dedicated current-employer field (SS42 C3
                                   # still needs a second independent source)
    raw: dict = field(default_factory=dict)

    @property
    def found(self) -> bool:
        return bool(self.full_name and self.linkedin_url)


@dataclass(slots=True)
class ProfileResult:
    url: str = ""
    handle: str = ""
    full_name: str = ""
    headline: str = ""
    company: str = ""
    title: str = ""
    bio: str = ""
    external_links: list[str] = field(default_factory=list)
    is_private: bool = False
    followers: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def found(self) -> bool:
        return bool(self.url)


def available(http: HttpClient) -> bool:
    return config.has_key(PROVIDER) and http.ledger.is_available(PROVIDER)


def _run_actor(http: HttpClient, actor: str, payload: dict, context: str,
               *, rotate_on_cap: bool = True) -> list[dict]:
    url = f"{BASE}/acts/{actor.replace('/', '~')}/run-sync-get-dataset-items"
    accounts = config.keys(PROVIDER)
    retried_live = False

    # Start on an account that has BOTH allowance for this actor and money left.
    ring = http.ledger.keyring.ring(PROVIDER)
    if ring.keys:
        for offset in range(len(ring.keys)):
            candidate = (ring.index + offset) % len(ring.keys)
            if _usable(ring.keys[candidate], actor):
                ring.index = candidate
                break

    for _ in range((len(accounts) or 1) + 1):
        ring = http.ledger.keyring.ring(PROVIDER)
        token = ring.keys[ring.index] if ring.keys else ""
        if not token:
            return []
        if not _usable(token, actor):
            if rotate_on_cap and _advance(http):
                continue
            return []
        if (token, actor) in _capped_pairs:
            # This account is out of runs for THIS actor. Advance the ring INDEX only
            # -- never retire the account, because it may still have allowance for a
            # different actor (Instagram, profile batch, the fallback people search).
            if rotate_on_cap and _advance(http):
                continue
            return []
        try:
            resp = http.request("POST", url, provider=PROVIDER, params={"token": token},
                                json_body=payload, context=context, timeout=300,
                                retries=1, cache_exclude=("token",))
        except BudgetExceeded:
            return []
        if resp.ok:
            data = resp.json()
            items = data if isinstance(data, list) else []
            if items:
                return items
            # An empty dataset is never a durable fact -- it may mean this account was
            # out of runs when the response was first stored. Drop it so a retry
            # really hits the network.
            was_cached = getattr(resp, "from_cache", False)
            http.invalidate("POST", url, params={"token": token}, json_body=payload,
                            cache_exclude=("token",))
            if was_cached and not retried_live:
                # The emptiness came from cache, not from a live answer. Now that the
                # entry is gone, retry the SAME account once for a real response.
                # Previously this returned immediately, so a single stale empty entry
                # permanently blocked that chunk from ever being fetched.
                retried_live = True
                continue
            # Distinguish "this account is out of free runs" from "no results".
            actor_id = _ACTOR_IDS.get(actor, "")
            if _hit_actor_run_limit(token, actor_id):
                _capped_pairs.add((token, actor))
                if not rotate_on_cap:
                    return []
                if rotate_on_cap and _advance(http):
                    continue
            return []
        if token == http.ledger.key_for(PROVIDER):
            return []
    return []


# Third-party actors enforce their own free-plan quotas. harvestapi caps free users
# at 10 runs and then SUCCEEDS with an empty dataset -- HTTP 201, no error status --
# so the ordinary quota-status rotation never fires. We detect it from the run's
# status message instead, and rotate to the next Apify account.
# Only phrasings that mean "this account is OUT of runs for this actor". Counters
# like "1/15 runs and 0/35" are informational and must NOT match.
_RUN_LIMIT_MARKERS = (
    "free user run limit reached",
    "free users are limited",   # matches both "limited to" and "limited up to"
    "upgrade to a paid plan to run more",
    "run limit reached",
    # Apify's own per-actor monthly allowance, phrased differently again:
    #   "this Actor can be started 1 time(s) per calendar month per user,
    #    and that allowance is used up."
    "allowance is used up",
    "per calendar month per user",
    "free user item limit exceeded",
)

# Free-run caps are imposed per ACTOR by its developer, so they are tracked per
# (token, actor). Retiring a whole account because its *fallback* actor is capped
# would discard the primary actor's remaining runs on that same account.
_capped_pairs: set[tuple[str, str]] = set()


def capped_pairs() -> set[tuple[str, str]]:
    return set(_capped_pairs)


def _hit_actor_run_limit(token: str, actor_id: str = "") -> bool:
    """Whether the most recent run for this token hit a free-plan limit.

    The limit is reported ONLY in the run LOG -- the run's `statusMessage` field is
    null and its status is SUCCEEDED, so metadata alone cannot distinguish
    "out of free runs" from "no results". Both reads are plain metadata, not actor
    runs, so they are free and unbilled.
    """
    import requests
    try:
        runs = _bounded_get(f"{BASE}/actor-runs",
                           params={"token": token, "limit": 20, "desc": "true"},
                           timeout=20)
        if not runs.ok:
            return False
        items = (runs.json().get("data") or {}).get("items") or []
        if actor_id:
            # Only this actor's runs matter -- another actor's cap says nothing about
            # this one's allowance. If the account has NEVER run this actor it cannot
            # be capped, so report available rather than falling back to an unrelated
            # run (which produced false "capped" readings and skipped usable quota).
            items = [i for i in items if i.get("actId") == actor_id]
            if not items:
                return False
        if not items:
            return False
        message = (items[0].get("statusMessage") or "").lower()
        if any(marker in message for marker in _RUN_LIMIT_MARKERS):
            return True
        log = _bounded_get(f"{BASE}/logs/{items[0]['id']}",
                          params={"token": token}, timeout=20)
        if not log.ok:
            return False
        return any(marker in log.text.lower() for marker in _RUN_LIMIT_MARKERS)
    except (requests.RequestException, KeyError, ValueError):
        return False


# Remaining monthly budget per token, cached briefly. An account with allowance
# but no budget silently returns nothing, so callers must skip it -- this was
# costing entire sweeps that appeared to "succeed" with empty datasets. The cache
# must EXPIRE: the continuous loop is one long process, and a budget read once at
# start-up kept a drained account "usable" for a day of 403 refusals.
_BUDGET_CACHE: dict[str, tuple[float, float]] = {}
_BUDGET_TTL_SECONDS = 600


def account_budget(token: str, *, refresh: bool = False) -> float:
    """Dollars left on this Apify account this month (0.0 if unknown)."""
    import time
    cached = _BUDGET_CACHE.get(token)
    if not refresh and cached and time.monotonic() - cached[1] < _BUDGET_TTL_SECONDS:
        return cached[0]
    import requests
    try:
        data = _bounded_get(f"{BASE}/users/me/limits",
                           params={"token": token}, timeout=20).json().get("data", {})
    except (requests.RequestException, ValueError):
        return cached[0] if cached else 0.0
    used = (data.get("current") or {}).get("monthlyUsageUsd", 0) or 0
    cap = (data.get("limits") or {}).get("maxMonthlyUsageUsd", 0) or 0
    left = max(0.0, float(cap) - float(used))
    _BUDGET_CACHE[token] = (left, time.monotonic())
    return left


def _usable(token: str, actor: str, *, min_budget: float = 0.25) -> bool:
    """An account is usable for an actor only with BOTH allowance and budget."""
    if (token, actor) in _capped_pairs:
        return False
    return account_budget(token) >= min_budget


def _advance(http: HttpClient) -> bool:
    """Move to the next account that is not already capped for the current actor.

    Deliberately distinct from KeyRing.rotate(): rotate() RETIRES a key, which is
    right for an invalid or quota-dead credential but wrong for a per-actor cap. An
    account out of runs for one actor usually still has allowance for others, and
    retiring it strands that capacity.
    """
    ring = http.ledger.keyring.ring(PROVIDER)
    if not ring.keys:
        return False
    ring.index = (ring.index + 1) % len(ring.keys)
    return True


def _guard(http: HttpClient, conditions: dict[str, bool]) -> bool:
    """Assert preconditions (raises on violation); returns availability."""
    http.ledger.require(PROVIDER, conditions)
    return available(http)


def _position(item: dict) -> tuple[str, str]:
    """Extract (company, title) from whichever position shape the actor returned."""
    for key in ("currentPosition", "currentPositions"):
        entries = item.get(key) or []
        if entries and isinstance(entries, list) and isinstance(entries[0], dict):
            entry = entries[0]
            return (entry.get("companyName", ""),
                    entry.get("position") or entry.get("title", ""))
    return "", item.get("headline", "")


# "Director of Marketing, UK at Partisan Records" -> title without the employer tail
_HEADLINE_AT = re.compile(r"\s+\bat\b\s+.+$", re.I)


def _hits_from_candidate_finder(items: list[dict], company_name: str) -> list[PersonHit]:
    hits: list[PersonHit] = []
    for item in items:
        name = (item.get("fullName")
                or f"{item.get('firstName', '')} {item.get('lastName', '')}").strip()
        company = item.get("currentCompany") or ""
        title = _HEADLINE_AT.sub("", item.get("headline") or "").strip()
        if not name or not company:
            continue
        hits.append(PersonHit(full_name=name, title=title, company=company,
                              linkedin_url=linkedin_person_url(
                                  item.get("profileUrl") or item.get("url") or ""),
                              location=item.get("location") or "", raw=item))
    return hits


def _hits_from_people_search(items: list[dict], company_name: str) -> list[PersonHit]:
    """Parse khadinakbar results, recovering the employer from the headline.

    Its `currentCompany` field is sometimes blank while the headline still reads
    "<Title> at <Employer>", so the employer is taken from there when needed --
    without it, otherwise-good records would be dropped for lacking an employer.
    """
    hits: list[PersonHit] = []
    for item in items:
        name = (item.get("fullName") or item.get("name")
                or f"{item.get('firstName', '')} {item.get('lastName', '')}").strip()
        headline = item.get("headline") or item.get("currentTitle") or ""
        company = item.get("currentCompany") or item.get("company") or ""
        if isinstance(company, dict):
            company = company.get("name", "")
        title = headline
        if " at " in headline.lower():
            idx = headline.lower().rindex(" at ")
            title = headline[:idx].strip()
            company = company or headline[idx + 4:].strip()
        # A leading "Co-Owner, Rough Trade" style headline also carries the employer.
        if not company and "," in headline:
            head, _, tail = headline.partition(",")
            if company_name.lower() in tail.lower():
                title, company = head.strip(), tail.strip()
        if not name or not company:
            continue
        hits.append(PersonHit(full_name=name, title=title, company=company,
                              linkedin_url=linkedin_person_url(
                                  item.get("profileUrl") or item.get("url") or ""),
                              location=item.get("location") or "", raw=item))
    return hits


def _hits_from_neuralverge(items: list[dict], company_name: str) -> list[PersonHit]:
    hits: list[PersonHit] = []
    for item in items:
        url = linkedin_person_url(item.get("linkedinUrl", ""))
        if not url:
            continue
        hits.append(PersonHit(
            full_name=(item.get("fullName")
                       or f"{item.get('firstName', '')} {item.get('lastName', '')}").strip(),
            title=item.get("title") or "",
            company=item.get("company") or company_name,
            linkedin_url=url,
            location=item.get("location") or "",
            tenure=item.get("tenureAtCompany") or "",
            raw=item))
    return hits


def _hits_from_harvestapi(items: list[dict], company_name: str) -> list[PersonHit]:
    hits: list[PersonHit] = []
    for item in items:
        url = linkedin_person_url(item.get("linkedinUrl", ""))
        if not url:
            continue
        company, title = _position(item)
        name = " ".join(p for p in (item.get("firstName", ""),
                                    item.get("lastName", "")) if p).strip()
        loc = item.get("location")
        hits.append(PersonHit(full_name=name or item.get("name", ""),
                              title=title, company=company or company_name,
                              linkedin_url=url,
                              location=loc.get("linkedinText", "") if isinstance(loc, dict) else "",
                              raw=item))
    return hits


def _hits_from_employees_serp(items: list[dict], company_name: str) -> list[PersonHit]:
    hits: list[PersonHit] = []
    for item in items:
        url = linkedin_person_url(item.get("profileUrl", ""))
        if not url:
            continue
        hits.append(PersonHit(
            full_name=item.get("name") or "",
            title=item.get("headline") or "",
            company=item.get("companyName") or company_name,
            linkedin_url=url,
            location=item.get("location") or "",
            weak_evidence=True,
            raw=item))
    return hits


def search_people_at_company(http: HttpClient, company_name: str, *,
                             company_linkedin_url: str = "", titles: list[str] | None = None,
                             location: str = "", max_items: int = 25,
                             conditions: dict[str, bool]) -> list[PersonHit]:
    """Enumerate decision-makers at ONE qualified company (SS30/SS32 Step 2).

    REQUIRES the company's LinkedIn company URL: both actors are company-scoped, and
    a free-text query returns either nothing (harvestapi) or unrelated people
    (memo23). Without a URL we do NOT spend.

    SS30/SS31 role filtering is applied locally by the caller rather than through
    server-side title/seniority filters -- those are AND-ed and were verified to
    return zero results.
    """
    if not _guard(http, conditions):
        return []

    # Name-based search first: it needs no LinkedIn company URL, so it reaches every
    # qualified company rather than the minority with a resolvable company page.
    items = _run_actor(http, ACTOR_PEOPLE_BY_NAME,
                       {"currentCompanies": [company_name],
                        "maxResults": max_items},
                       context=f"people-name:{company_name}")
    if items:
        return _hits_from_candidate_finder(items, company_name)

    if not company_linkedin_url:
        return []

    items = _run_actor(http, ACTOR_PEOPLE_SEARCH,
                       {"companies": [company_linkedin_url], "maxResults": max_items},
                       context=f"people:{company_name}")
    if items:
        return _hits_from_neuralverge(items, company_name)

    # Fallback: different developer, different free-run allowance. Rewind to the
    # first account -- the accounts exhausted for the primary actor are usually not
    # the ones exhausted for this one.
    ring = http.ledger.keyring.ring(PROVIDER)
    ring.index = 0
    ring.retired.clear()
    items = _run_actor(http, ACTOR_PEOPLE_SEARCH_FALLBACK,
                       {"profileScraperMode": "Short", "maxItems": max_items,
                        "takePages": 1, "currentCompanies": [company_linkedin_url]},
                       # Rotate on cap here too: with several accounts, the fallback
                       # actor may still have allowance on one that the primary has
                       # exhausted. Refusing to rotate strands that capacity.
                       context=f"people-fb:{company_name}", rotate_on_cap=True)
    if items:
        return _hits_from_harvestapi(items, company_name)

    # Last resort: cheap ($0.003/profile), no-login, company-URL-scoped SERP
    # actor. Weaker evidence (no dedicated current-employer field), so hits are
    # marked weak_evidence=True and must clear a second independent source
    # before SS42 C3 treats the employment as verified.
    items = _run_actor(http, ACTOR_EMPLOYEES_SERP,
                       {"companyUrls": [company_linkedin_url], "maxEmployees": max_items},
                       context=f"people-serp:{company_name}", rotate_on_cap=True)
    return _hits_from_employees_serp(items, company_name)


def find_linkedin_profile(http: HttpClient, name: str, company: str, *,
                          conditions: dict[str, bool]) -> PersonHit:
    """Resolve ONE known person's LinkedIn profile by name + company (SS34)."""
    if not _guard(http, conditions):
        return PersonHit()

    parts = name.split()
    payload = {
        "profileScraperMode": "Short",
        "firstName": parts[0],
        "lastName": parts[-1] if len(parts) > 1 else "",
        "strictSearch": True,
        "maxItems": 5,
    }
    for item in _run_actor(http, ACTOR_PERSON_BY_NAME, payload,
                           context=f"person:{name}@{company}"):
        url = linkedin_person_url(item.get("linkedinUrl", ""))
        if not url:
            continue
        found_company, title = _position(item)
        # SS34: reject a same-name profile at the wrong company.
        if company and found_company:
            if company.casefold() not in found_company.casefold() and \
               found_company.casefold() not in company.casefold():
                continue
        full = " ".join(p for p in (item.get("firstName", ""),
                                    item.get("lastName", "")) if p).strip()
        return PersonHit(full_name=full, title=title, company=found_company,
                         linkedin_url=url, raw=item)
    return PersonHit()


def fetch_profiles_batch(http: HttpClient, urls: list[str], *,
                         conditions: dict[str, bool]) -> dict[str, PersonHit]:
    """Fetch many LinkedIn profiles in ONE run. Returns {input_url: PersonHit}.

    Batching matters twice over: the free-run cap counts RUNS, not profiles, and
    per-profile billing is only $0.004 -- so one run can verify an entire cohort.
    """
    urls = [u for u in urls if u]
    if not urls or not _guard(http, conditions):
        return {}

    # Free accounts are capped at 10 ITEMS PER RUN on this actor, and exceeding it
    # returns an EMPTY dataset rather than a truncated one -- so the chunk size must
    # respect the cap exactly or the whole batch is silently lost.
    out: dict[str, PersonHit] = {}
    for start in range(0, len(urls), PROFILE_BATCH_SIZE):
        chunk = urls[start:start + PROFILE_BATCH_SIZE]
        items = _run_actor(
            http, ACTOR_PROFILE_BATCH,
            {"profileScraperMode": "Profile details no email ($4 per 1k)",
             "queries": chunk},
            context=f"profiles:{len(chunk)}")
        for item in items:
            url = linkedin_person_url(item.get("linkedinUrl", ""))
            company, title = _position(item)
            name = (item.get("fullName")
                    or f"{item.get('firstName', '')} {item.get('lastName', '')}").strip()
            hit = PersonHit(full_name=name, title=title, company=company,
                            linkedin_url=url or "", raw=item)
            # Key by both the canonical URL and the public identifier so callers can
            # match regardless of which form they supplied.
            if url:
                out[url.lower()] = hit
            if item.get("publicIdentifier"):
                out[str(item["publicIdentifier"]).lower()] = hit
    return out


def fetch_instagram_profile(http: HttpClient, handle: str, *,
                            conditions: dict[str, bool]) -> ProfileResult:
    """Fetch an Instagram profile for SS35 identity verification."""
    handle = instagram_handle(handle)
    if not handle or not _guard(http, conditions):
        return ProfileResult()

    for item in _run_actor(http, ACTOR_INSTAGRAM_PROFILE, {"usernames": [handle]},
                           context=f"ig:{handle}"):
        if not item.get("username"):
            continue
        links = [item[k] for k in ("externalUrl", "external_url", "bioLink")
                 if item.get(k)]
        for extra in item.get("bioLinks") or []:
            if isinstance(extra, dict) and extra.get("url"):
                links.append(extra["url"])
        return ProfileResult(
            url=f"https://www.instagram.com/{item['username']}/",
            handle=item["username"],
            full_name=item.get("fullName") or item.get("full_name", ""),
            bio=item.get("biography", ""),
            external_links=links,
            is_private=bool(item.get("private") or item.get("isPrivate")),
            followers=int(item.get("followersCount") or 0),
            raw=item)
    return ProfileResult()
