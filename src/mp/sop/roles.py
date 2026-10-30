"""SS30 decision-maker role families and SS31 person-level exclusions.

The hard requirement is SS30 "Equivalent Titles":

    Required family: Head of Digital
    Actual title:    Director, Digital Strategy
    -> may still be highly relevant. "Focus on actual function and responsibility."

So matching cannot be a lookup table of exact strings. A title is decomposed into
a SENIORITY marker and a FUNCTION keyword, which are scored independently. That
generalises to wordings the SOP never lists ("Global Head of Audience", "VP,
Streaming Strategy") without ever inventing relevance for an unrelated role.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .. import config
from ..normalize import _fold


@dataclass(slots=True)
class TitleMatch:
    family: str = ""
    seniority: str = ""
    relevance: int = 0
    matched_function: str = ""
    matched_seniority: str = ""
    excluded: bool = False
    exclusion_reason: str = ""
    confidence: str = "none"     # exact | derived | weak | none

    @property
    def qualifies(self) -> bool:
        return bool(self.family) and not self.excluded


# Seniority markers, most senior first. Order matters: "managing director" must be
# tested before "director".
_SENIORITY_MARKERS: list[tuple[str, str]] = [
    ("chief executive officer", "executive"), ("chief operating officer", "executive"),
    ("chief marketing officer", "executive"), ("chief revenue officer", "executive"),
    ("chief digital officer", "executive"), ("chief content officer", "executive"),
    ("managing director", "executive"), ("managing partner", "executive"),
    ("executive director", "executive"),   # SS30.1 equivalent leadership title
    ("general manager", "executive"), ("co founder", "executive"),
    ("cofounder", "executive"), ("founder", "executive"), ("owner", "executive"),
    ("president", "executive"), ("proprietor", "executive"),
    ("ceo", "executive"), ("coo", "executive"), ("cmo", "executive"),
    ("cro", "executive"), ("cdo", "executive"),
    # Non-English equivalents. SS30 says to focus on actual function, and the
    # approved geography (SS7) spans DE/AT/CH, FR, ES, PT, IT, NL and the Nordics,
    # where LinkedIn titles are usually in the local language. Without these, the
    # single most senior person at a European company is silently missed --
    # e.g. Napalm Records' "Geschaftsfuhrer" (Managing Director).
    ("geschaftsfuhrer", "executive"), ("geschaftsfuhrerin", "executive"),
    ("inhaber", "executive"), ("grunder", "executive"), ("mitgrunder", "executive"),
    ("directeur general", "executive"), ("gerant", "executive"),
    ("fondateur", "executive"), ("cofondateur", "executive"),
    ("director general", "executive"), ("gerente general", "executive"),
    ("fundador", "executive"), ("cofundador", "executive"), ("socio", "executive"),
    ("amministratore delegato", "executive"), ("fondatore", "executive"),
    ("directeur", "senior"), ("direttore", "senior"), ("diretor", "senior"),
    ("oprichter", "executive"), ("eigenaar", "executive"),
    ("verkstallande direktor", "executive"), ("grundare", "executive"),
    ("adm direktor", "executive"), ("daglig leder", "executive"),
    ("global head", "senior"), ("group head", "senior"),
    ("executive vice president", "senior"), ("senior vice president", "senior"),
    ("vice president", "senior"), ("evp", "senior"), ("svp", "senior"),
    ("vp", "senior"), ("head", "senior"),
    ("senior director", "senior"), ("director", "senior"),
    ("partner", "senior"),
    ("senior manager", "mid_senior"), ("lead", "mid_senior"),
    ("senior", "mid_senior"), ("manager", "mid_senior"),
    ("associate", "limited"), ("specialist", "limited"), ("executive", "limited"),
    ("coordinator", "junior"), ("assistant", "junior"), ("intern", "junior"),
    ("junior", "junior"), ("trainee", "junior"),
]

# Function keywords -> role family (SS30.1-SS30.7). Longest phrases first.
_FUNCTION_KEYWORDS: list[tuple[str, str]] = [
    # SS30.2 digital & marketing
    ("audience development", "digital_marketing"), ("artist marketing", "digital_marketing"),
    ("digital strategy", "digital_marketing"), ("digital marketing", "digital_marketing"),
    ("streaming strategy", "digital_marketing"), ("audience growth", "digital_marketing"),
    ("marketing", "digital_marketing"), ("digital", "digital_marketing"),
    ("marknadsforing", "digital_marketing"), ("mercadotecnia", "digital_marketing"),
    ("audience", "digital_marketing"), ("streaming", "digital_marketing"),
    ("dsp", "digital_marketing"), ("growth", "digital_marketing"),
    # SS30.3 label & release
    ("label operations", "label_release"), ("release management", "label_release"),
    ("label manager", "label_release"), ("campaign management", "label_release"),
    ("campaigns", "label_release"), ("campaign", "label_release"),
    ("release", "label_release"), ("label", "label_release"),
    # SS30.4 A&R and artist development
    ("artist development", "anr_artist_development"), ("a and r", "anr_artist_development"),
    ("a r", "anr_artist_development"), ("anr", "anr_artist_development"),
    ("talent", "anr_artist_development"), ("repertoire", "anr_artist_development"),
    # SS30.6 artist / label services and partnerships
    ("artist services", "artist_label_services"), ("label services", "artist_label_services"),
    ("partnerships", "artist_label_services"), ("partnership", "artist_label_services"),
    ("commercial", "artist_label_services"), ("business development", "artist_label_services"),
    # SS30.5 artist management
    ("artist management", "artist_management"), ("artist manager", "artist_management"),
    ("management", "artist_management"),
    # SS30.7 operations
    ("operations", "operations"), ("ops", "operations"),
]

# SS31 titles that are excluded even at a strong company.  The sole safe overlap is
# a genuine company leader who is also credited as a producer (for example,
# "Founder & Producer").  Words such as CEO/President appearing in an assistant's
# title describe the person they support, not the assistant's own seniority.
def _can_override_exclusion(reason: str, seniority: str) -> bool:
    return seniority == "executive" and reason.endswith("'producer'")


def _families() -> dict:
    return config.sop()["role_families"]


def _find_seniority(folded: str) -> tuple[str, str]:
    for marker, tier in _SENIORITY_MARKERS:
        if re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", folded):
            return tier, marker
    return "", ""


def _find_function(folded: str) -> tuple[str, str]:
    for keyword, family in _FUNCTION_KEYWORDS:
        if re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", folded):
            return family, keyword
    return "", ""


def check_person_exclusion(title: str) -> tuple[bool, str]:
    """SS31. Returns (excluded, reason)."""
    folded = _fold(title)
    if not folded:
        return False, ""
    for term in config.sop()["person_exclusions"]["titles"]:
        t = _fold(term)
        if t and re.search(rf"(?<!\w){re.escape(t)}(?!\w)", folded):
            return True, f"SS31 person-level exclusion: '{term}'"
    return False, ""


def classify_title(title: str, *, small_company: bool = False) -> TitleMatch:
    """Map a job title onto an SS30 role family, honouring SS30 equivalent titles.

    `small_company` implements the SS30.2 note: "A Digital Marketing Manager may
    qualify in a smaller organization when the role is sufficiently senior and
    clearly covers multiple artists."
    """
    folded = _fold(title)
    if not folded:
        return TitleMatch()

    families = _families()

    # 1. Exact match against a listed SS30 title -> highest confidence.
    for family, spec in families.items():
        for listed in spec["titles"]:
            if folded == _fold(listed):
                excluded, reason = check_person_exclusion(title)
                if excluded and not _can_override_exclusion(reason, spec["seniority"]):
                    return TitleMatch(excluded=True, exclusion_reason=reason,
                                      confidence="exact")
                return TitleMatch(family=family, seniority=spec["seniority"],
                                  relevance=spec["relevance"], matched_function=listed,
                                  confidence="exact")

    # 2. Decompose into seniority + function (SS30 equivalent titles).
    seniority, sen_marker = _find_seniority(folded)
    family, func_kw = _find_function(folded)

    excluded, reason = check_person_exclusion(title)
    # A genuine Founder/Owner may also be credited as a producer.  No other SS31
    # exclusion is overridden by a senior word elsewhere in the title.
    if excluded and not _can_override_exclusion(reason, seniority):
        return TitleMatch(excluded=True, exclusion_reason=reason, confidence="derived")

    # 3. Leadership with no functional keyword (SS30.1: Founder, CEO, Owner...).
    if not family and seniority == "executive":
        spec = families["leadership"]
        return TitleMatch(family="leadership", seniority="executive",
                          relevance=spec["relevance"], matched_seniority=sen_marker,
                          confidence="derived")

    if not family:
        extra = _supplementary_match(folded, small_company)
        if extra:
            fam, sen, rel = extra
            return TitleMatch(family=fam, seniority=sen, relevance=rel,
                              matched_function=folded, confidence="derived")
        return TitleMatch(confidence="none")

    # A function keyword alone is not a job title. Marketplace and listing text
    # ("White Label Test Pressings") contains SS30 vocabulary and would otherwise
    # be classified as a decision-maker. Require a seniority marker, unless the
    # string exactly matches a listed SS30 title (handled above).
    if not seniority:
        extra = _supplementary_match(folded, small_company)
        if extra:
            fam, sen, rel = extra
            return TitleMatch(family=fam, seniority=sen, relevance=rel,
                              matched_function=folded, confidence="derived")
        return TitleMatch(confidence="none")

    spec = families[family]
    # Seniority from the title itself wins; fall back to the family's default.
    effective = seniority or spec["seniority"]

    # SS31 excludes junior staff outright. Matching on the derived seniority tier
    # rather than on English keywords catches localised titles too
    # ("Junior Online Marketing Managerin").
    if effective == "junior":
        return TitleMatch(excluded=True,
                          exclusion_reason=f"SS31: junior-level role ('{title.strip()}')",
                          confidence="derived")

    # SS30.2 / SS30.5: manager-level roles need a small company to qualify.
    relevance = spec["relevance"]
    confidence = "derived"
    if effective in ("mid_senior",) and not small_company:
        relevance = max(relevance - 3, 0)
        confidence = "weak"
    if effective in ("limited", "junior"):
        relevance = max(relevance - 6, 0)
        confidence = "weak"

    return TitleMatch(family=family, seniority=effective, relevance=relevance,
                      matched_function=func_kw, matched_seniority=sen_marker,
                      confidence=confidence)


# Person SOP 02 v4.0 SS4 role matrix titles the SS30 vocabulary misses. Added
# 2026-10-19 after company-page harvesting showed genuine decision-makers being
# dropped: a UK "Director" is a company director, and label "Product Managers" run
# releases. Finance/HR/legal/tech chiefs stay out (SS5).
_CHIEF = re.compile(r"\bchief (?:([a-z]+) )?officer\b")
_CHIEF_EXCLUDED = {"financial", "finance", "technology", "technical", "information", "legal",
                   "people", "human", "data", "security", "medical", "accounting"}
_BARE_DIRECTOR = re.compile(r"^(?:company |managing |label |music |)director(?:\b.*)?$")
_PRODUCT = re.compile(r"^(?:senior )?product manager\b")
_PRESS = re.compile(r"\b(?:head of (?:press|pr|promotion|promotions|publicity)|"
                    r"(?:press|promotion|promotions|publicity) director)\b")


# Person SOP 02 v4.0 SS4.1 -- Projects LIVE promoters (added 2026-10-25). PRIMARY:
# ticketing / ticket-sales / revenue heads and the promoter role itself; ALTERNATE:
# festival / live-events heads. Booking-only, box office and production stay out.
_LIVE_COMMERCIAL = re.compile(
    r"\b(?:(?:head|director|vp|vice president) of (?:ticketing|ticket sales|revenue|live revenue)|"
    r"(?:ticketing|ticket sales|revenue) (?:director|manager|lead))\b")
_LIVE_LEAD = re.compile(
    r"\b(?:festival director|(?:head|director|vp|vice president) of (?:live|live events|"
    r"live music|festivals?|events|concerts|touring)|(?:live|concerts?|touring|events) director|"
    r"general manager,? live)\b")
_PROMOTER = re.compile(r"^(?:senior |head |lead |chief |national |regional )?promoter\b|"
                       r"\bhead promoter\b|\bconcert promoter\b")
_PARTNER = re.compile(r"^(?:founding |managing |senior )?partner\b")


def _supplementary_match(folded: str, small_company: bool) -> tuple[str, str, int] | None:
    if _LIVE_COMMERCIAL.search(folded):
        return "artist_label_services", "senior", 10
    if _PROMOTER.search(folded):
        return "artist_label_services", "senior", 9
    if _LIVE_LEAD.search(folded):
        return "leadership", "senior", 9
    if _PARTNER.match(folded):
        return "leadership", "executive", 9
    m = _CHIEF.search(folded)
    if m and (m.group(1) or "") not in _CHIEF_EXCLUDED:
        return "leadership", "executive", 10
    if _PRESS.search(folded):
        return "digital_marketing", "senior", 8
    if _PRODUCT.match(folded):
        return "label_release", "mid_senior", 8
    # A bare "Director" only implies company leadership at a small company.
    if small_company and _BARE_DIRECTOR.match(folded) and not any(
            w in folded for w in ("creative", "artistic", "music director", "art director",
                                  "film", "video", "photo", "editorial", "finance")):
        return "leadership", "executive", 9
    return None
