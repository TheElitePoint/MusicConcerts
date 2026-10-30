"""Name/URL/handle normalization and the SS52 duplicate-control keys.

SS52 defines the duplicate-check order explicitly:
  Company: 1) official website/domain  2) company Instagram  3) company LinkedIn  4) normalized name
  Person:  1) personal LinkedIn URL    2) personal Instagram 3) full name + company 4) current title

The SOP also warns that "Example Records", "Example Records Ltd" and "Example Music Records"
may all be the same company -- but says to *verify before creating separate records*. So we
emit two name keys: a STRICT key (safe to auto-merge on) and a LOOSE key (a merge *candidate*
that must be confirmed, never auto-merged).
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse, urlunparse

import tldextract

# tldextract without live PSL fetches -- keeps the pipeline offline-testable.
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())

# Corporate/legal suffixes carry no identity. Stripped from BOTH keys.
_LEGAL_SUFFIXES = {
    "ltd", "limited", "llc", "inc", "incorporated", "corp", "corporation", "co",
    "gmbh", "mbh", "ug", "ag", "bv", "nv", "sarl", "sas", "sa", "srl", "spa",
    "ab", "as", "aps", "oy", "oyj", "plc", "pty", "kft", "spzoo", "kk", "kg",
    "eood", "ood", "doo", "sl", "slu", "cv", "vof",
}

# Industry descriptors. Stripped ONLY from the loose key -- these often *do*
# distinguish real companies ("Foo Records" vs "Foo Management").
_DESCRIPTORS = {
    "records", "record", "recordings", "recording", "music", "musik", "musique",
    "musica", "group", "label", "labels", "entertainment", "media", "productions",
    "production", "publishing", "management", "mgmt", "agency", "company",
    "collective", "sound", "sounds", "audio", "the",
}

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_AMP_RE = re.compile(r"\s*&\s*")


def _fold(text: str) -> str:
    """Lowercase, strip accents, collapse punctuation and whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _AMP_RE.sub(" and ", text.lower())
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def normalize_company_name(name: str) -> str:
    """STRICT key: folded, legal suffixes removed. Safe to auto-merge on."""
    tokens = [t for t in _fold(name).split() if t not in _LEGAL_SUFFIXES]
    return " ".join(tokens)


def loose_company_key(name: str) -> str:
    """LOOSE key: also strips industry descriptors. A merge *candidate* only.

    'Example Records' / 'Example Records Ltd' / 'Example Music Records' -> 'example'
    Never auto-merge on this; SS52 requires verification first.
    """
    tokens = [t for t in _fold(name).split()
              if t not in _LEGAL_SUFFIXES and t not in _DESCRIPTORS]
    return " ".join(tokens) or normalize_company_name(name)


def normalize_person_name(name: str) -> str:
    """Folded person name with honorifics and trailing credentials removed."""
    drop = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "phd", "mba"}
    tokens = [t for t in _fold(name).split() if t not in drop]
    return " ".join(tokens)


def registrable_domain(url_or_host: str) -> str:
    """Return the registrable domain ('sub.example.co.uk' -> 'example.co.uk').

    This is the PRIMARY company identity key per SS52.
    """
    if not url_or_host:
        return ""
    candidate = str(url_or_host).strip()
    if "//" not in candidate:
        candidate = "//" + candidate
    host = urlparse(candidate).netloc or urlparse(candidate).path
    host = host.split("@")[-1].split(":")[0].strip().lower()
    ext = _EXTRACT(host)
    if not ext.domain or not ext.suffix:
        return ""
    return f"{ext.domain}.{ext.suffix}"


def canonical_url(url: str) -> str:
    """Strip tracking params, fragments and trailing slashes for stable caching."""
    if not url:
        return ""
    p = urlparse(url.strip())
    scheme = p.scheme or "https"
    netloc = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/") or "/"
    return urlunparse((scheme, netloc, path, "", "", ""))


_IG_RE = re.compile(r"(?:instagram\.com/)?@?([A-Za-z0-9_.]{1,30})/?$")
# Reserved paths that are never a profile handle.
_IG_RESERVED = {"p", "reel", "reels", "stories", "explore", "tv", "accounts", "direct"}


def instagram_handle(value: str) -> str:
    """Extract a bare Instagram handle from a URL or an @handle. '' if not derivable."""
    if not value:
        return ""
    value = str(value).strip().split("?")[0].rstrip("/")
    if "instagram.com" in value.lower():
        parts = [p for p in urlparse(
            value if "//" in value else "//" + value).path.split("/") if p]
        if not parts or parts[0].lower() in _IG_RESERVED:
            return ""
        value = parts[0]
    m = _IG_RE.match(value.lstrip("@"))
    return m.group(1).lower() if m else ""


def instagram_url(handle_or_url: str) -> str:
    h = instagram_handle(handle_or_url)
    return f"https://www.instagram.com/{h}/" if h else ""


def linkedin_slug(url: str) -> tuple[str, str]:
    """Return (kind, slug) where kind is 'in' (person) or 'company', else ('', '').

    The slug keeps its ORIGINAL CASE. Public vanity slugs are lowercase anyway, but
    LinkedIn also issues obfuscated member URNs ("ACwAAAQ0LdIB6OF3...") which are
    case-sensitive -- lowercasing one produces a dead link. Callers that need a
    case-insensitive identity (dedupe keys) fold it themselves.

    SS53 forbids submitting a company page where a person profile is required,
    so the caller can assert on `kind`.
    """
    if not url:
        return "", ""
    u = str(url).strip().split("?")[0].rstrip("/")
    parts = [p for p in urlparse(u if "//" in u else "//" + u).path.split("/") if p]
    for i, part in enumerate(parts):
        low = part.lower()
        if low in ("in", "pub") and i + 1 < len(parts):
            return "in", parts[i + 1]
        if low in ("company", "school", "showcase") and i + 1 < len(parts):
            return "company", parts[i + 1].lower()
    return "", ""


def is_person_linkedin(url: str) -> bool:
    return linkedin_slug(url)[0] == "in"


def linkedin_person_url(url: str) -> str:
    kind, slug = linkedin_slug(url)
    return f"https://www.linkedin.com/in/{slug}" if kind == "in" else ""


# --------------------------------------------------------------------------
# SS52 dedupe keys -- ordered strongest-first. The first matching key wins.
# --------------------------------------------------------------------------

def company_dedupe_keys(*, website: str = "", instagram: str = "",
                        linkedin: str = "", name: str = "") -> list[tuple[str, str]]:
    """Ordered (kind, value) identity keys for a company, per SS52."""
    keys: list[tuple[str, str]] = []
    if d := registrable_domain(website):
        keys.append(("domain", d))
    if h := instagram_handle(instagram):
        keys.append(("instagram", h))
    kind, slug = linkedin_slug(linkedin)
    if kind == "company" and slug:
        keys.append(("linkedin", slug))
    if n := normalize_company_name(name):
        keys.append(("name", n))
    return keys


def person_dedupe_keys(*, linkedin: str = "", instagram: str = "",
                       name: str = "", company_id: str | int = "",
                       title: str = "") -> list[tuple[str, str]]:
    """Ordered (kind, value) identity keys for a person, per SS52."""
    keys: list[tuple[str, str]] = []
    kind, slug = linkedin_slug(linkedin)
    if kind == "in" and slug:
        keys.append(("linkedin", slug.lower()))
    if h := instagram_handle(instagram):
        keys.append(("instagram", h))
    n = normalize_person_name(name)
    if n and company_id != "":
        keys.append(("name_company", f"{n}|{company_id}"))
    if n and title:
        keys.append(("name_title", f"{n}|{_fold(title)}"))
    return keys


# --------------------------------------------------------------------------
# Name distinctiveness -- guards against same-name employer collisions.
# --------------------------------------------------------------------------
_WORDS: set[str] = set()
MUSIC_WORDS = {"records", "recordings", "music", "label", "sounds", "sound", "tracks",
               "audio", "entertainment", "publishing", "management", "artists"}


def is_distinctive_company_name(name: str) -> bool:
    """A company name unlikely to be shared by unrelated businesses.

    Single dictionary words ("Atlantic", "Defected", "Fiction") and short names
    ("CAM", "DDS", "Jo & Co", "The Vault") collided with realtors, dentists and
    magazines in practice; names carrying a music word or an invented token did not.
    """
    if not _WORDS:
        try:
            _WORDS.update(w.strip().lower() for w in open("/usr/share/dict/words"))
        except OSError:
            _WORDS.add("")
    tokens = normalize_company_name(name).split()
    if any(t in MUSIC_WORDS for t in tokens) and len(tokens) >= 2:
        return True
    core = [t for t in tokens if t not in ("the", "and", "of")]
    if len(core) >= 2 and len(" ".join(core)) >= 10:
        # "Your Summer", "Victory Pool": ordinary words shared by many businesses.
        return not all(_is_word(t) for t in core)
    if len(core) != 1 or len(core[0]) < 6:
        return False
    return not _is_word(core[0])


def _is_word(word: str) -> bool:
    """Dictionary word, allowing plural / past-tense forms (civilians, defected)."""
    stems = {word, word[:-1], word[:-2], word[:-3] + "y", word[:-1] + "e"}
    return bool(stems & _WORDS)
