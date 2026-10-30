"""Shared dataclasses passed between pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class RawSignal:
    """One piece of evidence that a company is currently active (SS27).

    Emitted by S1 discovery sources BEFORE the company is resolved, so it carries
    a raw label string rather than a company_id.
    """
    raw_label: str
    signal_type: str            # key from sop.yaml activity_signal_categories
    description: str
    evidence_url: str
    observed_at: str            # ISO date of the activity itself
    source: str
    external_id: str
    artists: tuple[str, ...] = ()
    country_hint: str = ""      # ISO-2, unverified at this stage
    label_external_id: str = ""  # e.g. MusicBrainz label MBID


@dataclass(slots=True)
class Evidence:
    """A verifiable URL supporting a claim (SS38)."""
    kind: str                   # roster|activity|type|country|profile|identity|employment|title
    url: str
    source: str
    strength: str = "strong"    # strongest|strong|weak
    snippet: str = ""


@dataclass(slots=True)
class PersonCandidate:
    """A named individual found at a qualified company (SS30/SS32)."""
    full_name: str
    title: str
    source: str
    evidence_url: str = ""
    linkedin_url: str = ""
    instagram_url: str = ""
    title_family: str = ""
    seniority: str = ""
    role_relevance: int = 0
    evidence: list[Evidence] = field(default_factory=list)


@dataclass(slots=True)
class RosterEstimate:
    """SS6 roster finding. `display` is what lands in the sheet."""
    low: int | None = None
    high: int | None = None
    exact: int | None = None
    display: str = "REVIEW - roster unclear"
    evidence_url: str = ""
    source: str = ""

    @property
    def resolved(self) -> bool:
        return self.exact is not None or (self.low is not None and self.high is not None)

    @property
    def midpoint(self) -> int | None:
        if self.exact is not None:
            return self.exact
        if self.low is not None and self.high is not None:
            return (self.low + self.high) // 2
        return None
