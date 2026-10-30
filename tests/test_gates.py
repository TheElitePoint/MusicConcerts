"""SS29 Gates 1-8 and their routing semantics."""
import pytest

from mp.sop.gates import evaluate


def facts(**over):
    base = dict(name="Example Records", description="an independent record label",
                country="GB", company_type="record_label", roster_estimate=14,
                roster_resolved=True, artist_count=6, activity_signal_count=7,
                recent_activity_count=4, evidence_urls=5, strong_evidence_count=3)
    base.update(over)
    return base


class TestVerdicts:
    def test_strong_company_passes_all_eight(self):
        r = evaluate(facts())
        assert r.passed_all and r.verdict == "PASS"
        assert len(r.gates) == 8

    def test_any_fail_rejects(self):
        assert evaluate(facts(country="NG")).verdict == "REJECT"

    def test_unclear_routes_to_review_not_reject(self):
        """SS55: never guess -- an unresolved fact must not become a rejection."""
        r = evaluate(facts(roster_resolved=False, roster_estimate=None))
        assert r.verdict == "REVIEW"
        assert not r.failed


class TestIndividualGates:
    def test_g1_geography(self):
        assert evaluate(facts(country="NG")).first_failure().id == "G1"
        assert evaluate(facts(country=""))  .unclear[0].id == "G1"

    def test_g2_conditional_type_is_unclear(self):
        """SS4.5: concert/festival is not a default target type."""
        r = evaluate(facts(company_type="concert_festival"))
        assert any(g.id == "G2" and g.result == "UNCLEAR" for g in r.gates)

    def test_g3_hard_exclusion(self):
        r = evaluate(facts(description="a recording studio and mastering studio"))
        assert r.first_failure().id == "G3"

    def test_g4_excluded_brand(self):
        assert evaluate(facts(name="Sony Music Mexico")).first_failure().id == "G4"

    def test_g4_affiliate_carve_out_passes(self):
        """SS64: ownership alone must not reject."""
        r = evaluate(facts(name="Heavenly Recordings", parent_org="Universal Music Group"))
        assert not r.failed

    def test_g5_single_artist_is_unclear(self):
        r = evaluate(facts(artist_count=1, roster_resolved=False, roster_estimate=None))
        assert any(g.id == "G5" and g.result == "UNCLEAR" for g in r.gates)

    def test_g5_roster_counts_as_multi_artist_evidence(self):
        """A verified 14-artist roster proves multi-artist relevance on its own."""
        r = evaluate(facts(artist_count=0, roster_estimate=14, roster_resolved=True))
        assert any(g.id == "G5" and g.result == "PASS" for g in r.gates)

    @pytest.mark.parametrize("n,expected", [
        (3, "PASS"), (14, "PASS"), (20, "PASS"),
        (23, "PASS"),      # SS58: do not reject for slightly exceeding 20
        (28, "PASS"),      # SS5: still focused
        (150, "UNCLEAR"),  # SS5: very large roster -> lower priority
        (1, "UNCLEAR"),
    ])
    def test_g6_roster_bands(self, n, expected):
        r = evaluate(facts(roster_estimate=n, roster_resolved=True))
        gate = next(g for g in r.gates if g.id == "G6")
        assert gate.result == expected

    def test_g6_unresolved_roster_never_guesses(self):
        r = evaluate(facts(roster_resolved=False, roster_estimate=None))
        gate = next(g for g in r.gates if g.id == "G6")
        assert gate.result == "UNCLEAR" and "never guess" in gate.reason

    def test_g7_stale_activity_deprioritizes(self):
        r = evaluate(facts(recent_activity_count=0, activity_signal_count=3))
        gate = next(g for g in r.gates if g.id == "G7")
        assert gate.result == "UNCLEAR" and gate.action == "deprioritize"

    def test_g8_weak_evidence_is_unclear(self):
        r = evaluate(facts(strong_evidence_count=0, evidence_urls=1))
        gate = next(g for g in r.gates if g.id == "G8")
        assert gate.result == "UNCLEAR"


class TestReportShape:
    def test_as_dict_round_trip(self):
        d = evaluate(facts()).as_dict()
        assert set(d) == {"G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"}
        assert all({"name", "result", "reason", "action"} <= set(v) for v in d.values())
