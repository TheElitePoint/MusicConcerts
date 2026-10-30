"""Person SOP 02 v4.0 (2026-10-13) SS9.2 channel score.

Same 100 points and the same 90 floor as SS39-SS46, but the client's newer block
structure. The practical difference: roster SIZE is no longer a scored item (the
Company SOP SS7 makes 3-25 a preference, ">25 not automatically reject"), and
"documented current activity" is scored on whether current activity is evidenced,
not on how many releases landed in the last 90 days. Hard gates are unchanged and
still veto any total (SS9.1).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .scoring import FactEvaluator, check_hard_gates

FINAL_PASS, RESEARCH_FURTHER = 90, 85
PERSON_FIT_FLOOR, CURRENT_ROLE_FLOOR = 20, 6       # SS9.2 internal quality floor


@dataclass
class V4Score:
    subscores: dict = field(default_factory=dict)
    failed_gates: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.subscores.values())

    @property
    def person_fit(self) -> int:
        return sum(v for k, v in self.subscores.items() if k.startswith("C"))

    @property
    def verdict(self) -> str:
        if self.failed_gates:
            return "NOT FINAL"
        if (self.total >= FINAL_PASS and self.person_fit >= PERSON_FIT_FLOOR
                and self.subscores["C3_current_employer_title"] >= CURRENT_ROLE_FLOOR):
            return "FINAL PASS"
        return "RESEARCH FURTHER" if self.total >= RESEARCH_FURTHER else "NOT FINAL"


def _band(value: int, bands: list[tuple[int, int]]) -> int:
    """First (threshold, points) whose threshold value meets."""
    for threshold, points in bands:
        if value >= threshold:
            return points
    return 0


def score(facts: dict, channel: str) -> V4Score:
    ev = FactEvaluator(facts)
    f = lambda k: int(facts.get(k) or 0)  # noqa: E731
    s: dict[str, int] = {}

    # A_company_track (30): type 10; activity evidence 10; service/platform match 10.
    s["A1_company_type"] = 10 if ev.type_exact() else 8 if ev.type_relevant() else 4
    if facts.get("lane") == "LIVE":
        # Promoter / Live (6+ Events): each counted event carries its own promoter-of-
        # record URL (SS9 grade A/B event credit), and 6+ events is the P1 match.
        s["A2_activity_evidence"] = 10 if f("live_event_count") >= 6 else 0
        s["A3_platform_match"] = 10 if f("live_event_count") >= 6 else 0
    else:
        s["A2_activity_evidence"] = _band(f("activity_evidence_strength"), [(3, 10), (2, 8), (1, 5)])
        s["A3_platform_match"] = _band(f("artists_evidenced"), [(5, 10), (3, 8), (2, 5)])

    # B_timing_activity (25): documented current activity 10; matched signal 10; corroboration 5.
    s["B1_current_activity"] = (10 if ev.signals_within_days(180) >= 1
                                else 6 if ev.signals_within_days(365) >= 1 else 0)
    s["B2_matched_signal"] = _band(f("distinct_signal_categories"), [(2, 10), (1, 7)])
    s["B3_corroboration"] = (5 if f("credible_activity_sources") >= 2
                             else 3 if f("credible_activity_sources") == 1
                             else 1 if f("weak_activity_sources") >= 1 else 0)

    # C_person_fit (25): function 10; seniority/influence 8; current employer/title proof 7.
    s["C1_functional_responsibility"] = _band(f("role_relevance"), [(10, 10), (8, 8), (5, 4), (0, 1)])
    s["C2_seniority_influence"] = {"executive": 8, "senior": 7, "mid_senior": 6,
                                   "limited": 3}.get(facts.get("seniority") or "", 1)
    s["C3_current_employer_title"] = (7 if f("employment_sources") >= 2
                                      else 5 if f("employment_sources") == 1
                                      else 3 if ev.employment_claimed() else 0)

    # D_evidence (10): company/opportunity trail 5; person identity/title sources 5.
    if facts.get("lane") == "LIVE":
        # Event records establish the operator; its own website completes the trail.
        s["D1_company_trail"] = 5 if facts.get("company_website") else 4
    else:
        s["D1_company_trail"] = _band(f("company_evidence_strength"), [(3, 5), (2, 4), (1, 2)])
    s["D2_person_sources"] = _band(f("employment_sources"), [(2, 5), (1, 3)])

    # E_channel (10): exact personal account 5; usability 5.
    s["E1_exact_account"] = 5 if facts.get(f"{channel}_verified") else 0
    s["E2_usability"] = {"full": 5, "partial": 4, "weak": 2}.get(
        facts.get(f"{channel}_usability") or "", 0)

    return V4Score(subscores=s, failed_gates=check_hard_gates(facts, channel))
