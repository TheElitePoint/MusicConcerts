"""SS39-SS46 qualification scoring.

Design rule: the score is a pure function of STRUCTURED FACTS. Nothing here reads
prose, and nothing here is produced by a language model. Every band in
config/scoring.yaml carries a `when` expression evaluated against a facts dict, so
the scoring policy can be audited and changed without touching Python.

SS46 is enforced as a veto: hard gates are checked independently, and a failed gate
forces NOT FINAL no matter how high the arithmetic total is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import config

# "name", "name >= 3", "name == executive", "fn(1,2) >= 3"
_EXPR = re.compile(r"""^\s*(?P<name>\w+)
                       (?:\((?P<args>[^)]*)\))?
                       (?:\s*(?P<op>>=|<=|==|!=|>|<)\s*(?P<value>.+?))?\s*$""",
                   re.VERBOSE)


@dataclass(slots=True)
class ScoreBreakdown:
    channel: str
    subscores: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    total: int = 0
    verdict: str = "NOT FINAL"
    failed_gates: list[str] = field(default_factory=list)

    @property
    def is_final_pass(self) -> bool:
        return self.verdict == "FINAL PASS"

    def as_dict(self) -> dict:
        return {"channel": self.channel, "subscores": self.subscores,
                "total": self.total, "verdict": self.verdict,
                "failed_gates": self.failed_gates, "reasons": self.reasons}


class FactEvaluator:
    """Evaluates a band's `when` expression against a facts dict.

    Deliberately NOT eval(): the expressions come from a config file, and a tiny
    explicit grammar keeps scoring auditable and immune to config injection.
    """

    def __init__(self, facts: dict):
        self.facts = facts

    # -- helper predicates referenced by scoring.yaml ----------------------
    def roster_between(self, lo, hi) -> bool:
        n = self.facts.get("roster_estimate")
        return n is not None and int(lo) <= int(n) <= int(hi)

    def roster_other(self) -> bool:
        n = self.facts.get("roster_estimate")
        return n is None or not (3 <= int(n) <= 30)

    def signals_within_days(self, days) -> int:
        buckets = self.facts.get("signals_by_age") or {}
        return sum(v for k, v in buckets.items() if int(k) <= int(days))

    def type_exact(self) -> bool:
        return (self.facts.get("type_confidence") == "strong"
                and bool(self.facts.get("company_type")))

    def type_relevant(self) -> bool:
        return (self.facts.get("type_confidence") == "moderate"
                and bool(self.facts.get("company_type")))

    def type_weak(self) -> bool:
        return True     # terminal band

    def employment_claimed(self) -> bool:
        return bool(self.facts.get("employment_claimed"))

    def always(self) -> bool:
        return True

    # -- expression evaluation --------------------------------------------
    def _resolve(self, name: str, args: str | None):
        if hasattr(self, name):
            fn = getattr(self, name)
            if callable(fn):
                parsed = [a.strip() for a in args.split(",")] if args else []
                return fn(*parsed)
        return self.facts.get(name)

    def evaluate(self, expression: str) -> bool:
        m = _EXPR.match(expression or "")
        if not m:
            return False
        left = self._resolve(m.group("name"), m.group("args"))
        op, raw = m.group("op"), m.group("value")
        if op is None:
            return bool(left)

        raw = raw.strip().strip("'\"")
        try:
            right: object = int(raw)
            left = int(left or 0)
        except (ValueError, TypeError):
            right = raw
            left = str(left or "")

        return {">=": lambda: left >= right, "<=": lambda: left <= right,
                ">": lambda: left > right, "<": lambda: left < right,
                "==": lambda: left == right, "!=": lambda: left != right}[op]()


def _score_section(evaluator: FactEvaluator, spec: dict) -> tuple[int, str]:
    """First matching band wins; bands are ordered best-first in scoring.yaml."""
    for band in spec["bands"]:
        if evaluator.evaluate(band["when"]):
            return int(band["points"]), band.get("desc", "")
    return 0, "No band matched"


def score(facts: dict, channel: str) -> ScoreBreakdown:
    """Compute the channel-specific score (SS40-SS44) and apply the SS46 veto."""
    cfg = config.scoring()
    ev = FactEvaluator(facts)
    out = ScoreBreakdown(channel=channel)

    for key, spec in cfg["sections"].items():
        points, reason = _score_section(ev, spec)
        out.subscores[key] = points
        out.reasons[key] = reason

    channel_cfg = cfg["channel_readiness"][channel]
    for key, spec in channel_cfg.items():
        if not isinstance(spec, dict) or "bands" not in spec:
            continue
        points, reason = _score_section(ev, spec)
        out.subscores[f"E_{key}"] = points
        out.reasons[f"E_{key}"] = reason

    out.total = sum(out.subscores.values())
    out.failed_gates = check_hard_gates(facts, channel)

    thresholds = cfg["thresholds"]
    if out.failed_gates:
        # SS46: "A numerical score cannot override a failed hard gate."
        out.verdict = "NOT FINAL"
    elif out.total >= thresholds["final_pass"]:
        out.verdict = "FINAL PASS"
    elif out.total >= thresholds["research_further"]:
        out.verdict = "RESEARCH FURTHER"
    else:
        out.verdict = "NOT FINAL"
    return out


def check_hard_gates(facts: dict, channel: str) -> list[str]:
    """SS46 hard gates. Returns the list of gate names that FAILED."""
    failed: list[str] = []

    if not facts.get("approved_country"):
        failed.append("approved_country")
    if not facts.get("company_type"):
        failed.append("correct_company_category")
    if facts.get("hard_excluded"):
        failed.append("no_hard_company_exclusion")
    if facts.get("brand_excluded"):
        failed.append("not_excluded_core_brand")
    if facts.get("lane") == "LIVE":
        # Company SOP 01 v4.0 SS4: a promoter needs 6+ distinct promoted events with
        # per-event proof, not an artist roster.
        if int(facts.get("live_event_count") or 0) < 6:
            failed.append("live_six_events")
    else:
        if int(facts.get("artists_evidenced") or 0) < 2:
            failed.append("multi_artist_relevance")
        if not facts.get("roster_evidence_url"):
            failed.append("credible_roster_evidence")
    if int(facts.get("recent_activity_count") or 0) < 1:
        failed.append("meaningful_current_activity")
    if not facts.get("person_name"):
        failed.append("relevant_named_person")
    if not facts.get("current_company_verified"):
        failed.append("current_company_verified")
    if not facts.get("current_title_verified"):
        failed.append("current_title_verified")
    if int(facts.get("evidence_urls") or 0) < 1:
        failed.append("supporting_evidence_urls")
    if facts.get("unresolved_duplicate"):
        failed.append("no_unresolved_duplicate")

    # SS44: each sheet requires ITS OWN channel profile, verified.
    if not facts.get(f"{channel}_verified"):
        failed.append("channel_profile_verified")
    return failed
