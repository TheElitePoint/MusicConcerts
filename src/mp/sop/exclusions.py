"""SS8 hard company exclusions and SS9 existing-relationship exclusions.

The SS9 logic is the subtle part. Three distinct outcomes, because the SOP draws a
careful line between an excluded brand and a merely-affiliated company:

  EXCLUDED  the name IS a core brand, or a direct regional variation of one
            ("Sony Music Mexico", "Universal Music UK")            -- SS63
  REVIEW    the name merely starts with a brand word but carries other meaningful
            tokens ("Make Believe Records"). Never auto-rejected, never auto-passed;
            SS55 forbids guessing either way.
  CLEAR     no relationship, OR a separately named affiliate that merely sits inside
            an excluded corporate group -- SS64 says research it independently.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .. import config
from ..normalize import _fold, normalize_company_name


@dataclass(slots=True)
class ExclusionResult:
    outcome: str            # EXCLUDED | REVIEW | CLEAR
    group: str = ""         # which SS8 group or 'existing_relationship'
    reason: str = ""
    matched: str = ""       # the term that triggered it

    @property
    def excluded(self) -> bool:
        return self.outcome == "EXCLUDED"


# Words that carry no identity in a company name: geography and generic descriptors.
# Used to decide whether "<Brand> <rest>" is a regional variation or a new company.
_GEO_WORDS: set[str] = set()
_DESCRIPTOR_WORDS = {
    "music", "musica", "musik", "records", "record", "recordings", "recording",
    "group", "entertainment", "label", "labels", "international", "worldwide",
    "global", "digital", "media", "publishing", "distribution", "company", "co",
    "the", "and", "of", "north", "south", "east", "west", "central", "latin",
    "america", "americas", "europe", "asia", "pacific", "nordic", "benelux", "iberia",
}


def _geo_words() -> set[str]:
    """Country names, ISO codes and common demonyms, folded to single tokens."""
    global _GEO_WORDS
    if _GEO_WORDS:
        return _GEO_WORDS
    words = {"uk", "usa", "us", "eu", "gb", "uae"}
    for iso, name in config.approved_countries().items():
        words.add(iso.lower())
        words.update(_fold(name).split())
    words.update({
        "american", "british", "english", "irish", "spanish", "portuguese", "french",
        "german", "dutch", "belgian", "swiss", "austrian", "italian", "swedish",
        "norwegian", "danish", "finnish", "icelandic", "polish", "czech", "estonian",
        "slovenian", "croatian", "greek", "australian", "kiwi", "japanese", "korean",
        "brazilian", "chilean", "mexican", "canadian", "brasil", "espana", "deutschland",
        "france", "italia", "nippon",
    })
    _GEO_WORDS = words
    return words


def _blob(*parts: str) -> str:
    return " ".join(_fold(p) for p in parts if p)


def _phrase_pattern(term: str) -> str:
    """Whole-phrase regex tolerant of plurals on the final word.

    SS8 keywords are written in the singular ("corporate event", "wedding") but real
    copy says "corporate events" and "weddings". Without this, whole-word matching
    silently misses the majority of excluded companies.
    """
    tokens = _fold(term).split()
    if not tokens:
        return ""
    body = r"\s+".join(re.escape(t) for t in tokens[:-1])
    last = re.escape(tokens[-1]) + r"(?:es|s)?"
    pattern = rf"{body}\s+{last}" if body else last
    return rf"(?<!\w){pattern}(?!\w)"


def check_hard_exclusion(name: str = "", description: str = "",
                         website_text: str = "") -> ExclusionResult:
    """SS8.1-SS8.10. Matches keywords and named companies against all available text.

    Called BEFORE detailed person research so budget is never spent on an excluded
    company. Matching is whole-phrase to avoid 'radio' hitting 'radiohead'.
    """
    haystack = _blob(name, description, website_text)
    if not haystack.strip():
        return ExclusionResult("CLEAR")

    for group, spec in config.sop()["hard_exclusions"].items():
        for key in ("named", "keywords"):
            for term in spec.get(key, []) or []:
                pattern = _phrase_pattern(term)
                if pattern and re.search(pattern, haystack):
                    return ExclusionResult("EXCLUDED", group, spec["reason"], term)
    return ExclusionResult("CLEAR")


def check_excluded_brand(name: str, parent: str = "", *, domain: str = "",
                         instagram: str = "", linkedin: str = "",
                         website_text: str = "") -> ExclusionResult:
    """SS9 existing-relationship exclusion, with the SS64 affiliate carve-out.

    The name alone is not enough. A major-owned imprint often keeps an independent-
    sounding name while its website and social accounts belong to the parent --
    e.g. "Mercury Records" served from islanddefjam.com with an
    instagram.com/universalmusicgroup profile. Those identifiers ARE the company's
    commercial identity under SS52, so they are checked too.
    """
    cfg = config.sop()["existing_relationships"]
    brands = cfg["core_brands"]
    folded_name = normalize_company_name(name)
    if not folded_name:
        return ExclusionResult("CLEAR")

    name_tokens = folded_name.split()
    noise = _geo_words() | _DESCRIPTOR_WORDS

    for brand in brands:
        brand_folded = normalize_company_name(brand)
        if not brand_folded:
            continue
        brand_tokens = brand_folded.split()

        # 1. Exact match -> excluded core brand.
        if folded_name == brand_folded:
            return ExclusionResult(
                "EXCLUDED", "existing_relationship",
                f"'{name}' is an excluded core brand (SS9).", brand)

        # 2. Brand as a leading token sequence -> inspect what follows.
        if name_tokens[:len(brand_tokens)] == brand_tokens:
            remainder = name_tokens[len(brand_tokens):]
            if all(tok in noise for tok in remainder):
                # Only geography/descriptors follow -> direct regional variation (SS63).
                return ExclusionResult(
                    "EXCLUDED", "existing_relationship",
                    f"'{name}' is a direct regional/brand variation of excluded core "
                    f"brand '{brand}' (SS9/SS63).", brand)
            # Meaningful extra tokens -> cannot decide safely (SS55: do not guess).
            return ExclusionResult(
                "REVIEW", "existing_relationship",
                f"'{name}' begins with excluded brand '{brand}' but carries additional "
                f"identity ({' '.join(remainder)}). Verify whether it is a regional "
                f"variation or a separately named company (SS9/SS64).", brand)

        # 3. Brand appears mid-name, e.g. "Made by Warner Music".
        if len(brand_tokens) == 1 and brand_tokens[0] in name_tokens:
            return ExclusionResult(
                "REVIEW", "existing_relationship",
                f"'{name}' contains excluded brand token '{brand}'. Verify whether it is "
                f"the same commercial entity (SS9).", brand)

    # 3b. Identity carriers: domain and official social handles. If a company's own
    # website or social profile IS an excluded brand, it is that brand's property
    # regardless of the imprint name it trades under (SS9/SS52).
    for brand in brands:
        brand_key = normalize_company_name(brand).replace(" ", "")
        if not brand_key or len(brand_key) < 4:
            continue
        for field, value in (("domain", domain), ("instagram", instagram),
                             ("linkedin", linkedin)):
            if not value:
                continue
            probe = _fold(value).replace(" ", "").replace("-", "").replace("_", "")
            if brand_key in probe:
                return ExclusionResult(
                    "EXCLUDED", "existing_relationship",
                    f"'{name}' operates under excluded core brand '{brand}' "
                    f"(its {field} is '{value}') (SS9).", brand)

    # 4. SS64 carve-out: parent ownership ALONE never excludes a separately named company.
    if parent:
        for brand in brands:
            if normalize_company_name(brand) in normalize_company_name(parent):
                return ExclusionResult(
                    "CLEAR", "existing_relationship",
                    f"Owned by/affiliated with '{parent}' but separately named. SS64: do not "
                    f"auto-reject -- research independently and qualify on its own merits.",
                    brand)
    return ExclusionResult("CLEAR")


def is_approved_country(iso2: str) -> bool:
    """SS7 geography gate."""
    return bool(iso2) and iso2.upper() in config.approved_countries()
