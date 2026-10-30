"""SS4 company-type classification -- rules first, LLM only for genuine ambiguity.

Groq is deliberately NOT the primary classifier. The SOP's categories are defined by
concrete vocabulary ("roster", "we manage", "distribution"), so keyword evidence is
both cheaper and more auditable than a model call. An LLM is consulted only when the
rules are genuinely undecided, and even then it may only choose among the SS4 enum
values -- it can never invent a category or a fact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .. import config
from ..normalize import _fold

# Vocabulary that distinguishes the SS4 categories beyond their bare aliases.
_SIGNALS: dict[str, list[tuple[str, int]]] = {
    "record_label": [
        ("record label", 6), ("independent label", 6), ("indie label", 5),
        ("our artists", 3), ("roster", 3), ("releases", 2), ("signed", 3),
        ("catalogue", 2), ("imprint", 4), ("we release", 5), ("new signing", 4),
        ("discography", 2), ("recordings", 2), ("album", 1), ("ep", 1),
    ],
    "artist_management": [
        ("artist management", 7), ("management company", 6), ("we manage", 6),
        ("management roster", 5), ("artist manager", 4), ("talent management", 5),
        ("we represent", 5), ("our roster", 3), ("day to day management", 5),
    ],
    "music_distributor": [
        ("music distribution", 7), ("distributor", 5), ("we distribute", 6),
        ("digital distribution", 6), ("distribution services", 5),
        ("physical distribution", 4), ("supply chain", 2), ("dsp", 2),
    ],
    "label_services": [
        ("label services", 7), ("artist services", 7), ("release services", 5),
        ("campaign services", 4), ("we work with labels", 4), ("client base", 3),
        ("artist development services", 5), ("project management", 2),
    ],
    "concert_festival": [
        ("festival", 6), ("concert promoter", 6), ("live music promoter", 6),
        ("tour promoter", 5), ("venue", 2), ("line-up", 3), ("lineup", 3),
    ],
}


@dataclass(slots=True)
class TypeResult:
    company_type: str = ""
    confidence: str = "none"      # strong | moderate | weak | none
    reason: str = ""
    scores: dict[str, int] | None = None

    @property
    def resolved(self) -> bool:
        return bool(self.company_type) and self.confidence in ("strong", "moderate")


def _score_text(blob: str) -> dict[str, int]:
    scores: dict[str, int] = {}
    types = config.sop()["company_types"]

    for ctype, spec in types.items():
        total = 0
        for alias in spec.get("aliases", []):
            folded = _fold(alias)
            if folded and re.search(rf"(?<!\w){re.escape(folded)}(?!\w)", blob):
                total += 4
        for phrase, weight in _SIGNALS.get(ctype, []):
            if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", blob):
                total += weight
        scores[ctype] = total
    return scores


def classify(name: str = "", description: str = "", website_text: str = "",
             *, has_roster: bool = False) -> TypeResult:
    """Classify a company into an SS4 category from its own words."""
    blob = _fold(" ".join(p for p in (name, description, website_text) if p))
    if not blob.strip():
        return TypeResult(reason="No text available to classify.")

    scores = _score_text(blob)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top, top_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0

    if top_score == 0:
        return TypeResult(scores=scores, reason="No SS4 category vocabulary matched.")

    margin = top_score - runner_up
    if top_score >= 10 and margin >= 4:
        confidence = "strong"
    elif top_score >= 6 and margin >= 2:
        confidence = "moderate"
    else:
        confidence = "weak"

    # A verified multi-artist roster corroborates label/management over services.
    if has_roster and top in ("record_label", "artist_management") and confidence == "weak":
        confidence = "moderate"

    label = config.sop()["company_types"][top]["label"]
    return TypeResult(company_type=top, confidence=confidence, scores=scores,
                      reason=f"Matched {label} vocabulary (score {top_score}, "
                             f"margin {margin} over next category).")
