"""SS39-SS46 scoring, driven by the SOP's illustrative examples (SS56-SS60)."""
import pytest

from mp.sop.scoring import FactEvaluator, check_hard_gates, score


def strong_facts(**over):
    """A record matching SS56 'Example North Records' -- should reach FINAL PASS."""
    base = dict(
        company_type="record_label", type_confidence="strong",
        roster_estimate=13, roster_evidence_url="https://example.com/artists",
        artists_evidenced=6,
        signals_by_age={"30": 2, "90": 4, "180": 5, "365": 6},
        distinct_signal_categories=3, credible_activity_sources=2,
        weak_activity_sources=0,
        role_relevance=10, seniority="senior", employment_sources=2,
        employment_claimed=True,
        company_evidence_strength=3, activity_evidence_strength=3,
        approved_country=True, hard_excluded=False, brand_excluded=False,
        recent_activity_count=4, person_name="Sarah Williams",
        current_company_verified=True, current_title_verified=True,
        evidence_urls=5, unresolved_duplicate=False,
        linkedin_verified=True, linkedin_usability="full",
        instagram_verified=True, instagram_usability="full",
    )
    base.update(over)
    return base


class TestSopWorkedExamples:
    def test_sop56_strong_label_reaches_final_pass(self):
        """SS56: illustrative LinkedIn score 98 -> FINAL PASS."""
        r = score(strong_facts(), "linkedin")
        assert r.verdict == "FINAL PASS"
        assert r.total >= 90

    def test_sop56_both_channels_eligible(self):
        """SS49/SS56: a person may enter BOTH sheets when both channels verify."""
        facts = strong_facts()
        assert score(facts, "linkedin").is_final_pass
        assert score(facts, "instagram").is_final_pass

    def test_sop58_larger_roster_still_passes(self):
        """SS58: 23 artists must NOT be rejected for exceeding 20."""
        r = score(strong_facts(roster_estimate=23), "linkedin")
        assert r.verdict == "FINAL PASS"

    def test_sop59_wrong_person_fails(self):
        """SS59: 'A good company cannot compensate for an irrelevant person.'"""
        r = score(strong_facts(role_relevance=0, seniority="junior",
                               employment_sources=0, employment_claimed=False), "linkedin")
        assert r.verdict != "FINAL PASS"

    def test_sop60_no_instagram_blocks_only_instagram(self):
        """SS60: LinkedIn Final YES, Instagram Final NO."""
        facts = strong_facts(instagram_verified=False, instagram_usability="none")
        assert score(facts, "linkedin").is_final_pass
        ig = score(facts, "instagram")
        assert not ig.is_final_pass
        assert "channel_profile_verified" in ig.failed_gates


class TestHardGateVeto:
    """SS46: 'A numerical score cannot override a failed hard gate.'"""

    @pytest.mark.parametrize("override,gate", [
        ({"approved_country": False}, "approved_country"),
        ({"hard_excluded": True}, "no_hard_company_exclusion"),
        ({"brand_excluded": True}, "not_excluded_core_brand"),
        ({"current_company_verified": False}, "current_company_verified"),
        ({"current_title_verified": False}, "current_title_verified"),
        ({"roster_evidence_url": ""}, "credible_roster_evidence"),
        ({"artists_evidenced": 1}, "multi_artist_relevance"),
        ({"recent_activity_count": 0}, "meaningful_current_activity"),
        ({"unresolved_duplicate": True}, "no_unresolved_duplicate"),
    ])
    def test_each_gate_blocks_final_pass(self, override, gate):
        r = score(strong_facts(**override), "linkedin")
        assert gate in r.failed_gates
        assert r.verdict == "NOT FINAL"

    def test_sop46_high_score_with_wrong_profile_is_not_final(self):
        """SS46 example: 'Score 93 but wrong LinkedIn profile -> NOT LinkedIn Final.'"""
        r = score(strong_facts(linkedin_verified=False), "linkedin")
        assert r.verdict == "NOT FINAL"


class TestThresholds:
    def test_85_to_89_is_research_further(self):
        """SS45: 85-89 -> RESEARCH FURTHER, never submitted."""
        # Shave points without tripping any hard gate.
        r = score(strong_facts(distinct_signal_categories=1,
                               credible_activity_sources=1,
                               company_evidence_strength=1), "linkedin")
        assert 85 <= r.total <= 89
        assert r.verdict == "RESEARCH FURTHER"

    def test_max_score_is_100(self):
        assert score(strong_facts(), "linkedin").total <= 100


class TestFactEvaluator:
    @pytest.mark.parametrize("expr,expected", [
        ("always", True),
        ("roster_between(3,20)", True),
        ("roster_between(21,25)", False),
        ("artists_evidenced >= 5", True),
        ("artists_evidenced >= 9", False),
        ("seniority == senior", True),
        ("seniority == executive", False),
        ("signals_within_days(90) >= 3", True),
        ("linkedin_verified", True),
    ])
    def test_expressions(self, expr, expected):
        assert FactEvaluator(strong_facts()).evaluate(expr) is expected

    def test_malformed_expression_is_false_not_crash(self):
        assert FactEvaluator({}).evaluate("!!! bad (((") is False

    def test_no_eval_injection(self):
        """Config is data, not code -- an expression can never execute Python."""
        assert FactEvaluator({}).evaluate("__import__('os').system('echo x')") is False
