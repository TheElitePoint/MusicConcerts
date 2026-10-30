"""Company website extraction -- SS24, and the strongest evidence tier in SS38.

"The official website should normally be one of the strongest verification sources."

Extracts the facts the SOP's gates and scoring actually need:
  * roster size and its evidence URL          (SS6  -> gate G6, score A2)
  * named team members with titles            (SS32 -> free person candidates)
  * official social profiles                  (SS37, SS47/SS48 columns)
  * self-description for type classification  (SS4  -> gate G2)
  * country signals                           (SS7  -> gate G1)

SS6 is emphatic that roster size must never be guessed. When a page cannot be counted
confidently the estimate stays unresolved and the record goes to REVIEW.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .. import config
from ..http import HttpClient
from ..models import PersonCandidate, RosterEstimate
from ..normalize import canonical_url, instagram_handle, linkedin_slug

PROVIDER = "website"

# Page-type detection from URL path and link text.
PAGE_PATTERNS = {
    "roster": r"(artists?|roster|talent|our-?artists|clients|management|signings)",
    "team": r"(team|about-?us|people|staff|who-?we-?are|contact|meet)",
    "about": r"(about|story|info|company)",
    "releases": r"(releases?|catalogue|catalog|music|discography)",
    "news": r"(news|blog|journal|press|updates)",
    "careers": r"(careers?|jobs|vacancies|work-?with-?us|hiring)",
    "contact": r"(contact|impressum|imprint|mentions-?legales|kontakt)",
}

# Link text that is navigation, never an artist or a person.
NAV_WORDS = {
    "home", "about", "contact", "news", "shop", "store", "cart", "search", "menu",
    "login", "sign in", "sign up", "subscribe", "newsletter", "privacy", "terms",
    "cookies", "faq", "help", "support", "back", "next", "previous", "more",
    "read more", "view all", "see all", "all artists", "artists", "roster", "team",
    "releases", "music", "events", "tour", "merch", "press", "blog", "careers",
    "instagram", "facebook", "twitter", "youtube", "spotify", "tiktok", "bandcamp",
    "soundcloud", "apple music", "linkedin", "email", "book", "booking", "demos",
    "submit", "policy", "accessibility", "sitemap", "legal", "imprint",
}

SOCIAL_HOSTS = {
    "instagram": "instagram.com", "linkedin": "linkedin.com",
    "youtube": "youtube.com", "facebook": "facebook.com",
    "twitter": "twitter.com", "bandcamp": "bandcamp.com",
    "spotify": "open.spotify.com", "soundcloud": "soundcloud.com",
    "tiktok": "tiktok.com",
}

# A job title next to a name on a team page.
TITLE_HINT = re.compile(
    r"\b(founder|co-?founder|ceo|coo|cmo|president|owner|director|head of|"
    r"managing|partner|manager|vp|vice president|a&r|marketing|digital|"
    r"operations|label|artist services|campaign|audience|general manager)\b",
    re.IGNORECASE)

PERSON_NAME = re.compile(r"^[A-Z][a-z'’\-]+(?: [A-Z][a-z'’\-]+){1,2}$")

# Words that never appear in a personal name. Without this, section headings like
# "Festival Archives" or "Vinyl Reissues" match the name pattern and a nearby
# category label ("Digital Distribution") matches the title pattern, producing
# convincing-looking people who do not exist.
NON_PERSON_TOKENS = {
    "records", "record", "recordings", "music", "musical", "archives", "archive",
    "festival", "festivals", "vinyl", "cd", "merch", "merchandise", "store", "shop",
    "collection", "collections", "catalogue", "catalog", "release", "releases",
    "news", "press", "media", "label", "labels", "group", "company", "limited",
    "digital", "distribution", "marketing", "management", "services", "studio",
    "studios", "sound", "audio", "live", "tour", "tours", "event", "events",
    "policy", "privacy", "terms", "cookie", "cookies", "contact", "about", "home",
    "account", "cart", "basket", "checkout", "search", "newsletter", "subscribe",
    "series", "edition", "editions", "box", "set", "sale", "new", "all", "more",
    # Call-to-action and nav phrasing that reads like a Title Case name:
    # "Concerts On Demand", "Musician Login", "Track Your Streams", "Know Your Audience".
    "concert", "concerts", "login", "logout", "signin", "register", "streams",
    "streaming", "audience", "demand", "tickets", "ticket", "playlist", "playlists",
    "download", "downloads", "stream", "track", "tracks", "know", "your", "our",
    "watch", "listen", "read", "learn", "join", "apply", "submit", "explore",
    "discover", "support", "donate", "membership", "member", "members", "account",
    # Role headings are titles, never names.
    "manager", "director", "founder", "owner", "president", "head", "vp",
    "officer", "executive", "coordinator", "operations", "leadership",
}


# Markers that a site is an e-commerce storefront rather than a company site.
# Critical: many labels put their MERCH STORE on the primary domain, where
# /collections/<artist> looks exactly like a roster but is a product category, and
# shipping copy names countries the company has no presence in.
STORE_MARKERS = ("cdn.shopify.com", "shopify", "add to cart", "/collections/",
                 "/products/", "woocommerce", "bigcartel", "checkout",
                 "shopping cart", "your basket")


@dataclass
class SiteData:
    base_url: str = ""
    pages: dict[str, str] = field(default_factory=dict)      # kind -> url
    html: dict[str, str] = field(default_factory=dict)       # url -> html
    description: str = ""
    text_blob: str = ""
    socials: dict[str, str] = field(default_factory=dict)
    roster: RosterEstimate = field(default_factory=RosterEstimate)
    team: list[PersonCandidate] = field(default_factory=list)
    country_hints: list[str] = field(default_factory=list)
    reachable: bool = False
    is_store: bool = False


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _classify(url: str, text: str = "") -> str:
    probe = f"{urlparse(url).path.lower()} {text.lower()}"
    for kind, pattern in PAGE_PATTERNS.items():
        if re.search(pattern, probe):
            return kind
    return ""


def fetch_site(http: HttpClient, url: str, *, max_pages: int | None = None) -> SiteData:
    """Fetch the homepage and the most relevant sub-pages."""
    cfg = config.sources()["verification"]["website"]
    max_pages = max_pages or cfg["max_pages_per_site"]
    timeout = cfg.get("timeout_seconds", 20)
    respect = cfg.get("respect_robots", True)

    data = SiteData(base_url=url)
    home = http.get(url, provider=PROVIDER, respect_robots=respect,
                    timeout=timeout, context="homepage")
    if not home.ok or not home.text:
        return data

    data.reachable = True
    data.html[canonical_url(url)] = home.text
    soup = _soup(home.text)

    meta = soup.find("meta", attrs={"name": "description"}) or \
        soup.find("meta", attrs={"property": "og:description"})
    data.description = (meta.get("content", "") if meta else "").strip()
    data.socials = extract_socials(soup, url)
    data.country_hints = extract_country_hints(soup)

    data.is_store = any(m in home.text.lower() for m in STORE_MARKERS)

    # Pick the best candidate URL per page kind, preferring dedicated pages over
    # storefront category pages (/artists/... beats /collections/...).
    scored: dict[str, tuple[int, str]] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if urlparse(href).netloc != urlparse(url).netloc:
            continue
        text = a.get_text(" ", strip=True)
        kind = _classify(href, text)
        if not kind:
            continue
        path = urlparse(href).path.lower()
        rank = 0
        if re.match(rf"^/{PAGE_PATTERNS[kind]}/?$", path):
            rank += 10                      # exact top-level section page
        if re.search(PAGE_PATTERNS[kind], path):
            rank += 5                       # section appears in the path at all
        if "/collections/" in path or "/products/" in path:
            rank -= 8                       # storefront category, not a roster
        if text and text.lower() in ("artists", "roster", "talent", "team", "about"):
            rank += 4
        if kind not in scored or rank > scored[kind][0]:
            scored[kind] = (rank, href)
    candidates = {k: v[1] for k, v in scored.items() if v[0] > 0}

    # SS6 requires real roster evidence, and many sites bury the roster link in a
    # JS menu the parser never sees. Probing the conventional paths directly is far
    # more reliable than trusting homepage navigation alone.
    if "roster" not in candidates:
        for path in ("/artists", "/artists/", "/roster", "/roster/", "/our-artists",
                     "/talent", "/artist", "/management"):
            probe = urljoin(url, path)
            resp = http.get(probe, provider=PROVIDER, respect_robots=respect,
                            timeout=timeout, context="probe:roster")
            if resp.ok and resp.text and len(resp.text) > 800:
                candidates["roster"] = probe
                break

    # SS7 geography almost always lives on the contact or (in DE/AT/CH) Impressum
    # page. Neither is reachable through the roster/team nav ranking, so probe the
    # conventional paths directly -- the same tactic the roster probe above uses.
    if "contact" not in candidates:
        for path in ("/contact", "/contact-us", "/kontakt", "/impressum",
                     "/imprint", "/legal", "/about/contact"):
            probe = urljoin(url, path)
            resp = http.get(probe, provider=PROVIDER, respect_robots=respect,
                            timeout=timeout, context="probe:contact")
            if resp.ok and resp.text and len(resp.text) > 400:
                candidates["contact"] = probe
                break

    for kind in ("roster", "team", "about", "contact", "releases", "news", "careers"):
        if kind not in candidates or len(data.html) >= max_pages:
            continue
        page = http.get(candidates[kind], provider=PROVIDER, respect_robots=respect,
                        timeout=timeout, context=f"page:{kind}")
        if page.ok and page.text:
            data.pages[kind] = candidates[kind]
            data.html[canonical_url(candidates[kind])] = page.text

    for page_html in data.html.values():
        data.country_hints.extend(extract_country_hints(_soup(page_html)))

    texts = [_soup(h).get_text(" ", strip=True) for h in data.html.values()]
    data.text_blob = " ".join(texts)[:20000]

    if "roster" in data.pages:
        roster_url = data.pages["roster"]
        # SS6: never guess. A storefront category page is not a roster, so we return
        # "unclear" rather than counting products as artists.
        if "/collections/" in roster_url.lower() or "/products/" in roster_url.lower():
            data.roster = RosterEstimate(evidence_url=roster_url, source=PROVIDER)
        else:
            data.roster = extract_roster(data.html[canonical_url(roster_url)], roster_url)
    if "team" in data.pages:
        data.team = extract_team(data.html[canonical_url(data.pages["team"])],
                                 data.pages["team"])
    if not data.team and "about" in data.pages:
        data.team = extract_team(data.html[canonical_url(data.pages["about"])],
                                 data.pages["about"])
    if not data.team:
        data.team = extract_team(home.text, url)
    return data


def extract_socials(soup: BeautifulSoup, base: str) -> dict[str, str]:
    """Official social profiles from nav/footer links (SS37, SS47/SS48 columns)."""
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        host = urlparse(href).netloc.lower()
        for name, needle in SOCIAL_HOSTS.items():
            if needle in host and name not in found:
                if name == "instagram" and not instagram_handle(href):
                    continue
                if name == "linkedin" and linkedin_slug(href)[0] != "company":
                    continue
                found[name] = href.split("?")[0]
    return found


def extract_country_hints(soup: BeautifulSoup) -> list[str]:
    """Country signals from the page: postal address markup and explicit country names."""
    hints: list[str] = []
    for tag in soup.select('[itemprop="addressCountry"], .country, address'):
        text = tag.get_text(" ", strip=True)
        if text:
            hints.append(text[:120])
    return hints


# Address-shaped patterns that identify a country far more reliably than a bare
# country name in body text. SS7 permits geography that is *established*; a
# registered-company number or a postal-code format is establishment, not inference.
GEO_PATTERNS: list[tuple[str, str]] = [
    # Company registration statements -- the strongest single free signal.
    (r"registered\s+in\s+england(?:\s+and\s+wales)?", "GB"),
    (r"registered\s+in\s+scotland", "GB"),
    (r"company\s+(?:registration\s+)?(?:no\.?|number)\s*:?\s*(?:sc)?\d{6,8}", "GB"),
    (r"\bvat\s+(?:reg(?:istration)?\s+)?(?:no\.?|number)\s*:?\s*gb\s*\d", "GB"),
    (r"\bhandelsregister\b|\bamtsgericht\b|\bust-?idnr\b|\bhrb\s*\d+", "DE"),
    (r"\bsiret\b|\bsiren\b|\brcs\s+paris\b", "FR"),
    (r"\bkvk[-\s]?(?:nummer)?\s*:?\s*\d{8}\b", "NL"),
    (r"\borganisationsnummer\b|\borg\.?\s*nr\.?\s*\d{6}-\d{4}", "SE"),
    (r"\bcvr[-\s]?(?:nr\.?)?\s*:?\s*\d{8}\b", "DK"),
    (r"\babn\b\s*:?\s*\d{2}\s?\d{3}\s?\d{3}\s?\d{3}", "AU"),
    # International dialling prefixes shown on contact pages.
    (r"\+44\s*\(?0?\)?\s*\d", "GB"), (r"\+49\s*\(?0?\)?\s*\d", "DE"),
    (r"\+33\s*\(?0?\)?\s*\d", "FR"), (r"\+31\s*\(?0?\)?\s*\d", "NL"),
    (r"\+46\s*\(?0?\)?\s*\d", "SE"), (r"\+47\s*\d", "NO"),
    (r"\+45\s*\d", "DK"), (r"\+358\s*\d", "FI"), (r"\+354\s*\d", "IS"),
    (r"\+353\s*\d", "IE"), (r"\+61\s*\d", "AU"), (r"\+64\s*\d", "NZ"),
    (r"\+81\s*\d", "JP"), (r"\+82\s*\d", "KR"), (r"\+39\s*\d", "IT"),
    (r"\+34\s*\d", "ES"), (r"\+351\s*\d", "PT"), (r"\+32\s*\d", "BE"),
    (r"\+43\s*\d", "AT"), (r"\+41\s*\d", "CH"), (r"\+48\s*\d", "PL"),
    (r"\+372\s*\d", "EE"),
    # Postal-code shapes anchored to a city/street context word.
    (r"\b[a-z]{1,2}\d[a-z\d]?\s+\d[a-z]{2}\b", "GB"),   # UK postcode
    (r"\b(?:d-)?\d{5}\s+(?:berlin|hamburg|m[uü]nchen|k[oö]ln|frankfurt)", "DE"),
    (r"\b\d{4}\s?[a-z]{2}\s+(?:amsterdam|rotterdam|utrecht)", "NL"),
]


def extract_geo_signals(text: str) -> Counter[str]:
    """ISO-2 votes from address-shaped patterns in contact/legal copy (SS7).

    Weighted 2 -- as strong as structured address markup, and deliberately stronger
    than a country name appearing in prose, which tour dates and distributor lists
    routinely contaminate.
    """
    votes: Counter[str] = Counter()
    blob = text.lower()
    for pattern, iso in GEO_PATTERNS:
        if re.search(pattern, blob):
            votes[iso] += 2
    return votes


def extract_roster(html: str, page_url: str) -> RosterEstimate:
    """Count artists on a roster page (SS6).

    Strategy: artist links on a roster page overwhelmingly share a URL prefix
    (/artist/<slug>, /artists/<slug>, /roster/<slug>). We count DISTINCT SLUGS under
    the largest roster-shaped prefix.

    Counting slugs rather than link text matters: real roster pages are usually
    image grids whose anchors carry no text at all, so a text-based count silently
    returns zero. The slug is always present.

    Degrades to an unresolved estimate rather than a guessed number (SS6).
    """
    soup = _soup(html)
    for tag in soup(["nav", "header", "footer", "script", "style"]):
        tag.decompose()

    groups: dict[str, set[str]] = defaultdict(set)
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        if urlparse(href).netloc != urlparse(page_url).netloc:
            continue
        parts = [p for p in urlparse(href).path.strip("/").split("/") if p]
        if len(parts) < 2:
            continue
        slug = parts[1].lower()
        # Skip pagination and utility slugs that share the roster prefix.
        if slug in NAV_WORDS or slug.isdigit() or len(slug) < 2:
            continue
        groups["/" + parts[0].lower()].add(slug)

    roster_groups = {prefix: slugs for prefix, slugs in groups.items()
                     if re.search(PAGE_PATTERNS["roster"], prefix)}
    if not roster_groups:
        return RosterEstimate(evidence_url=page_url, source=PROVIDER)

    best_prefix = max(roster_groups, key=lambda p: len(roster_groups[p]))
    count = len(roster_groups[best_prefix])

    # Too few to be a roster, or too many to be a focused music company (SS5).
    if count < 2 or count > 400:
        return RosterEstimate(evidence_url=page_url, source=PROVIDER)

    return RosterEstimate(exact=count, low=count, high=count,
                          display=f"{count} artists",
                          evidence_url=page_url, source=PROVIDER)


def extract_team(html: str, page_url: str) -> list[PersonCandidate]:
    """Named people with titles from a team/about page (SS32 Step 1).

    These are free person candidates -- the cheapest possible source, gathered
    before any paid profile resolution is considered.
    """
    soup = _soup(html)
    for tag in soup(["script", "style"]):
        tag.decompose()

    people: list[PersonCandidate] = []
    seen: set[str] = set()

    # Blocks that plausibly wrap one person: a short text container with both a
    # name-shaped string and a title-shaped string.
    for block in soup.find_all(["li", "article", "div", "section", "td", "figure"]):
        text = block.get_text("\n", strip=True)
        if not text or len(text) > 400:
            continue
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not (2 <= len(lines) <= 8):
            continue

        name = title = ""
        for i, line in enumerate(lines):
            if not PERSON_NAME.match(line) or line.lower() in NAV_WORDS:
                continue
            # Every token must be plausible as part of a person's name.
            if any(tok.lower() in NON_PERSON_TOKENS for tok in line.split()):
                continue
            for follower in lines[i + 1:i + 3]:
                if TITLE_HINT.search(follower) and len(follower) < 80:
                    name, title = line, follower
                    break
            if name:
                break

        key = name.lower()
        if name and title and key not in seen:
            seen.add(key)
            people.append(PersonCandidate(full_name=name, title=title,
                                          source="website_team",
                                          evidence_url=page_url))

    # Some first-party team directories render each field as a separate sibling:
    #
    #   DAVID MACIAS
    #   -
    #   Co-Founder, President
    #
    # No compact container then holds both fields, so the block parser above sees
    # nothing.  Parse that explicit name/separator/title sequence as a fallback.
    lines = [ln.strip() for ln in soup.get_text("\n", strip=True).splitlines()
             if ln.strip()]
    for i in range(len(lines) - 1):
        raw_name = lines[i]
        if i + 2 < len(lines) and lines[i + 1] in {"-", "–", "—"}:
            raw_title = lines[i + 2]
            title_index = i + 2
        elif re.search(r"\s[-–—]\s*$", raw_name):
            raw_name = re.sub(r"\s[-–—]\s*$", "", raw_name)
            raw_title = lines[i + 1]
            title_index = i + 1
        else:
            continue
        # A wrapped title such as "Senior" + "Project Manager" is joined once.
        if (raw_title.lower() in {"senior", "global", "digital"}
                and title_index + 1 < len(lines) and len(lines[title_index + 1]) < 80):
            raw_title = f"{raw_title} {lines[title_index + 1]}"

        name = " ".join(raw_name.split())
        title = " ".join(raw_title.split())
        tokens = [t.strip(".,'") for t in name.split()]
        if not (2 <= len(tokens) <= 5 and 2 <= len(name) <= 80 and len(title) <= 120):
            continue
        if any(tok.lower() in NON_PERSON_TOKENS for tok in tokens):
            continue
        if not all(re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", tok) for tok in tokens):
            continue
        if not TITLE_HINT.search(title):
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        people.append(PersonCandidate(full_name=name.title() if name.isupper() else name,
                                      title=title, source="website_team",
                                      evidence_url=page_url))
    return people


# SS27 signal vocabulary. The crawler already fetches the news and careers pages;
# mining them yields the DISTINCT signal categories that SS41 B2 scores. Detecting
# only release activity capped B2 at 7/10 for every company in the database.
_SIGNAL_PATTERNS = {
    "new_signing": re.compile(
        r"\b(new signing|newly signed|we(?:'ve| have) signed|signs to|joins the (?:label|roster)"
        r"|welcome to the (?:label|family|roster)|latest signing)\b", re.I),
    "roster_expansion": re.compile(
        r"\b(join(?:s|ed)? our roster|expand(?:s|ed|ing) (?:our|the) roster"
        r"|new addition to the roster|growing roster)\b", re.I),
    "hiring": re.compile(
        r"\b(we(?:'re| are) hiring|join our team|current vacanc|open position|now recruiting"
        r"|job opening|apply now|careers at)\b", re.I),
    "new_partnership": re.compile(
        r"\b(partners? with|partnership with|teams? up with|joint venture|collaborat\w+ with)\b",
        re.I),
    "new_distribution": re.compile(
        r"\b(distribution deal|distributed by|distribution partnership"
        r"|label services (?:deal|agreement)|signs distribution)\b", re.I),
    "market_expansion": re.compile(
        r"\b(opens? (?:a|an|its) (?:new )?office|expand(?:s|ing) into|launch(?:es|ing) in"
        r"|new division|international expansion)\b", re.I),
}


def detect_activity_signals(site: SiteData) -> dict[str, str]:
    """Find SS27 activity categories in the company's own news/careers/about copy.

    Returns {category: evidence_url}. Only the company's own site is used, so these
    land in SS38's "strongest" evidence tier.
    """
    found: dict[str, str] = {}
    for kind in ("news", "careers", "about", "roster"):
        url = site.pages.get(kind)
        if not url:
            continue
        html = site.html.get(canonical_url(url))
        if not html:
            continue
        text = _soup(html).get_text(" ", strip=True)[:20000]
        for category, pattern in _SIGNAL_PATTERNS.items():
            if category not in found and pattern.search(text):
                found[category] = url
    return found


def guess_country(site: SiteData, domain: str) -> tuple[str, str]:
    """Derive an ISO-2 country with a confidence label (SS7).

    SS7 forbids assuming geography, so signals are weighted by reliability:
      ccTLD                       2   (a .co.uk is strong, if not conclusive)
      explicit address markup     2
      country named in contact text 1

    Free body text is deliberately excluded -- tour dates and distributor
    mentions name many countries and would otherwise dominate the vote.
    "verified" requires a total of 3+, i.e. two genuinely independent signals.
    """
    names = config.country_name_to_iso()
    votes: Counter[str] = Counter()

    # A storefront's shipping/returns copy names many countries the company has no
    # presence in, so only the ccTLD is trustworthy there (SS7: never assume).
    store_only_cctld = site.is_store

    # 1. ccTLD
    cctld = {"UK": "GB", "DE": "DE", "FR": "FR", "NL": "NL", "SE": "SE", "NO": "NO",
             "DK": "DK", "FI": "FI", "IE": "IE", "ES": "ES", "PT": "PT", "IT": "IT",
             "AU": "AU", "NZ": "NZ", "CA": "CA", "JP": "JP", "KR": "KR", "BR": "BR",
             "CL": "CL", "MX": "MX", "PL": "PL", "CZ": "CZ", "AT": "AT", "CH": "CH",
             "BE": "BE", "GR": "GR", "IS": "IS", "EE": "EE", "SI": "SI", "HR": "HR"}
    tld = domain.rsplit(".", 1)[-1].upper() if domain else ""
    if tld in cctld:
        votes[cctld[tld]] += 2

    if store_only_cctld:
        if not votes:
            return "", "unknown"
        iso, score = votes.most_common(1)[0]
        return iso, "single_source"

    # 2. Structured address markup
    address_blob = " ".join(site.country_hints).lower()
    for name, iso in names.items():
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", address_blob):
            votes[iso] += 2

    # 3. Country named near contact details only
    contact_blob = ""
    for url, html in site.html.items():
        if re.search(r"(contact|about|impressum|imprint|kontakt|legal)", url, re.I):
            contact_blob += " " + _soup(html).get_text(" ", strip=True).lower()
    for name, iso in names.items():
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", contact_blob):
            votes[iso] += 1

    # 4. Address-shaped patterns (company registration, dialling code, postcode).
    # Scanned on contact/legal pages only, for the same reason free body text is
    # excluded above: a tour list names countries the company has no presence in.
    votes.update(extract_geo_signals(contact_blob))

    if not votes:
        return "", "unknown"
    iso, score = votes.most_common(1)[0]
    return iso, ("verified" if score >= 3 else "single_source")
