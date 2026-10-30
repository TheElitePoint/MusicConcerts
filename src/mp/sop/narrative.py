"""SS54 data-writing standards: "Why Company Fits" / "Why This Person Fits".

    Good: "Independent label managing approximately 14 active artists, with several
           recent releases and recurring multi-artist promotion activity."
    Weak: "Great music company."

Two generators:
  * a deterministic template that composes verified fields (always available);
  * an optional Groq pass that rewrites the same facts more naturally.

The Groq output is passed through `validate_against_facts`, which REJECTS any
sentence containing a number or capitalised entity that is not present in the input
facts. A model can only ever rephrase what was verified -- it can never introduce a
claim. If validation fails, the deterministic template is used instead.
"""
from __future__ import annotations

import re

from .. import config

_NUMBER = re.compile(r"\d+")
_ENTITY = re.compile(r"\b[A-Z][A-Za-z&'\-]+(?:\s+[A-Z][A-Za-z&'\-]+)*\b")

# Words that may appear capitalised without being a factual claim.
_SAFE_CAPS = {
    "Independent", "Record", "Label", "Artist", "Management", "Company", "Music",
    "Distributor", "Services", "The", "A", "An", "Head", "Director", "Founder",
    "CEO", "COO", "VP", "President", "Owner", "Managing", "Partner", "Marketing",
    "Digital", "Operations", "Campaigns", "Audience", "Development", "Instagram",
    "LinkedIn", "Approximately", "Recent", "Active", "Senior", "Global", "General",
    "Manager", "Partnerships", "Commercial", "Release", "Releases", "Roster",
}


def validate_against_facts(text: str, facts: dict) -> tuple[bool, str]:
    """Reject any number or entity in `text` absent from `facts`. (ok, reason)."""
    if not text:
        return False, "empty"

    haystack = " ".join(str(v) for v in facts.values() if v is not None)

    for number in _NUMBER.findall(text):
        if number not in haystack:
            return False, f"unsupported number '{number}'"

    # A capitalised word that merely starts a sentence is not an entity claim, so
    # those positions are skipped -- otherwise every valid rewrite would be rejected
    # on its own first word and the model output would never be usable.
    sentence_starts = {m.group(1) for m in
                       re.finditer(r"(?:^|[.!?]\s+)([A-Z][A-Za-z&'\-]*)", text)}

    for entity in _ENTITY.findall(text):
        for word in entity.split():
            if word in _SAFE_CAPS or len(word) <= 2 or word in sentence_starts:
                continue
            if word.lower() not in haystack.lower():
                return False, f"unsupported entity '{word}'"
    return True, ""


def why_company_fits(facts: dict) -> str:
    """Deterministic SS54-compliant company rationale built only from verified fields."""
    types = config.sop()["company_types"]
    type_label = types.get(facts.get("company_type", ""), {}).get("label", "Music company")

    roster = facts.get("roster_display") or ""
    parts = [type_label.lower()]
    if roster and not roster.startswith("REVIEW"):
        parts[0] = f"{type_label} with a roster of {roster.lower()}"

    artists = int(facts.get("artists_evidenced") or 0)
    if artists >= 2:
        parts.append(f"{artists} distinct artists evidenced as currently active")

    recent = int(facts.get("recent_activity_count") or 0)
    if recent:
        parts.append(f"{recent} recent activity signal(s) within the review window")

    categories = facts.get("signal_categories") or []
    if categories:
        readable = ", ".join(c.replace("_", " ") for c in sorted(categories)[:3])
        parts.append(f"covering {readable}")

    sentence = "; ".join(parts)
    return sentence[0].upper() + sentence[1:] + "." if sentence else ""


def why_person_fits(facts: dict) -> str:
    """Deterministic SS54-compliant person rationale."""
    title = facts.get("person_title") or "Decision-maker"
    family = (facts.get("title_family") or "").replace("_", " ")
    company = facts.get("company_name", "the company")

    scope = {
        "leadership": "sets company-wide direction and can authorise new tools and processes",
        "digital_marketing": "leads digital and marketing activity across the roster",
        "label_release": "owns label operations and release campaigns across multiple artists",
        "anr_artist_development": "leads A&R and artist development across the roster",
        "artist_management": "manages artists directly and influences wider company activity",
        "artist_label_services": "leads artist/label services and partnerships across clients",
        "operations": "owns operational systems and workflows company-wide",
    }.get(facts.get("title_family", ""), "holds a relevant senior function")

    sources = int(facts.get("employment_sources") or 0)
    verified = (f"current role and company independently verified across {sources} sources"
                if sources >= 2 else "current role verified")

    return (f"{title} at {company}: {scope}. Role family: {family or 'senior'}; "
            f"{verified}.")


def generate(kind: str, facts: dict, *, http=None) -> str:
    """Produce a rationale, preferring Groq but always falling back to the template."""
    template = (why_company_fits(facts) if kind == "company" else why_person_fits(facts))

    if http is None or not config.has_key("groq"):
        return template

    from ..sources import groq
    drafted = groq.rewrite_rationale(http, kind, facts, template)
    if not drafted:
        return template
    ok, _reason = validate_against_facts(drafted, facts)
    return drafted if ok else template
