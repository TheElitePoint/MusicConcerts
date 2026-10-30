"""Budget protection: caps, circuit breakers and Apify preconditions."""
import pytest

from mp.budget import BudgetExceeded, BudgetLedger, PreconditionFailed
from mp.db import Database


@pytest.fixture
def ledger(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    yield BudgetLedger(db, run_id="test")
    db.close()


def set_keys(ledger, provider, n):
    """Give a provider exactly n synthetic keys, independent of the real .env."""
    ring = ledger.keyring.ring(provider)
    ring.keys = [f"{provider}-key-{i}" for i in range(n)]
    ring.index = 0
    ring.retired = set()
    return ring


class TestKeyRotation:
    """Multiple keys must be exhausted one by one, not abandoned on first error."""

    def test_quota_error_rotates_instead_of_tripping(self, ledger):
        set_keys(ledger, "tavily", 5)
        ledger.record("tavily", "/search", status=429)
        ledger.check("tavily")                       # must NOT raise
        assert ledger.summary()["tavily"]["rotations"] == 1
        assert ledger.key_for("tavily") == "tavily-key-1"

    def test_trips_only_after_every_key_is_exhausted(self, ledger):
        set_keys(ledger, "tavily", 5)
        for _ in range(5):
            ledger.record("tavily", "/search", status=429)
        with pytest.raises(BudgetExceeded, match="exhausted"):
            ledger.check("tavily")
        assert ledger.is_available("tavily") is False

    def test_single_key_trips_on_first_quota_error(self, ledger):
        set_keys(ledger, "tavily", 1)
        ledger.record("tavily", "/search", status=402)
        with pytest.raises(BudgetExceeded):
            ledger.check("tavily")

    def test_server_error_does_not_burn_keys(self, ledger):
        """A 500 is the provider's problem, not the key's."""
        set_keys(ledger, "tavily", 5)
        ledger.record("tavily", "/search", status=500)
        assert ledger.summary()["tavily"]["rotations"] == 0
        assert ledger.key_for("tavily") == "tavily-key-0"

    def test_success_keeps_current_key(self, ledger):
        set_keys(ledger, "groq", 11)
        ledger.record("groq", "/chat", status=200)
        assert ledger.key_for("groq") == "groq-key-0"

    def test_rotation_survives_alternating_success(self, ledger):
        set_keys(ledger, "groq", 3)
        ledger.record("groq", "/chat", status=429)   # -> key-1
        ledger.record("groq", "/chat", status=200)
        ledger.record("groq", "/chat", status=429)   # -> key-2
        assert ledger.key_for("groq") == "groq-key-2"
        assert ledger.is_available("groq")


class TestCircuitBreakers:
    def test_apify_trips_after_consecutive_failures(self, ledger):
        set_keys(ledger, "apify", 1)
        for _ in range(3):
            ledger.record("apify", "/actor", status=500)
        with pytest.raises(BudgetExceeded):
            ledger.check("apify")

    def test_success_resets_failure_streak(self, ledger):
        set_keys(ledger, "apify", 1)
        ledger.record("apify", "/actor", status=500)
        ledger.record("apify", "/actor", status=200)
        ledger.record("apify", "/actor", status=500)
        ledger.check("apify")   # streak was reset, must not be tripped


class TestCaps:
    def test_per_run_cap_enforced(self, ledger):
        cap = ledger.spec("tavily")["per_run"]
        for _ in range(cap):
            ledger.check("tavily")
            ledger.record("tavily", "/search", status=200)
        with pytest.raises(BudgetExceeded, match="per-run cap"):
            ledger.check("tavily")

    def test_cache_hits_do_not_consume_budget(self, ledger):
        for _ in range(500):
            ledger.record("tavily", "/search", cache_hit=True, status=200)
        ledger.check("tavily")
        assert ledger.summary()["tavily"]["calls"] == 0

    def test_free_providers_have_no_month_cap(self, ledger):
        assert ledger.spec("musicbrainz")["per_month"] is None


class TestApifyPreconditions:
    """Apify must never be reachable from company discovery."""

    ALL_OK = {"company_passed_all_gates": True, "person_has_verified_name": True,
              "person_title_in_role_family": True, "no_cached_profile": True,
              "free_sources_exhausted": True}

    def test_all_satisfied_passes(self, ledger):
        ledger.require("apify", self.ALL_OK)

    @pytest.mark.parametrize("missing", list(ALL_OK))
    def test_any_missing_precondition_raises(self, ledger, missing):
        conds = dict(self.ALL_OK, **{missing: False})
        with pytest.raises(PreconditionFailed, match=missing):
            ledger.require("apify", conds)

    def test_empty_conditions_raise(self, ledger):
        with pytest.raises(PreconditionFailed):
            ledger.require("apify", {})


class TestCostTracking:
    def test_cost_accumulates_only_on_real_calls(self, ledger):
        ledger.record("apify", "/a", status=200)
        ledger.record("apify", "/a", cache_hit=True, status=200)
        assert ledger.total_cost() == pytest.approx(ledger.spec("apify")["cost_per_call"])

    def test_free_providers_cost_nothing(self, ledger):
        for p in ("musicbrainz", "deezer", "itunes", "wikidata", "website"):
            ledger.record(p, "/x", status=200)
        assert ledger.total_cost() == 0.0


class TestCacheKeySecrecy:
    """Credentials must never influence the cache key (see http.request)."""

    def test_rotating_a_key_still_hits_cache(self, ledger, tmp_path):
        from mp.http import HttpClient
        http = HttpClient(ledger, cache_dir=tmp_path / "cache")
        a = http._cache_key("POST", "https://api.x/search",
                            '{"api_key": "key-1", "query": "foo"}')
        b = http._cache_key("POST", "https://api.x/search",
                            '{"api_key": "key-2", "query": "foo"}')
        assert a != b, "sanity: raw bodies differ"

        # With api_key excluded, both keys must produce the SAME cache entry.
        import json as _json
        strip = lambda body: _json.dumps(
            {k: v for k, v in _json.loads(body).items() if k != "api_key"},
            sort_keys=True)
        assert (http._cache_key("POST", "https://api.x/search",
                                strip('{"api_key": "key-1", "query": "foo"}'))
                == http._cache_key("POST", "https://api.x/search",
                                   strip('{"api_key": "key-2", "query": "foo"}')))


class TestApifyAccountRotation:
    """harvestapi returns HTTP 201 + empty dataset when an account is out of free
    runs. Rotation must be driven by that condition, not by a status code."""

    def test_multiple_apify_accounts_are_loaded(self, ledger):
        set_keys(ledger, "apify", 2)
        assert ledger.key_for("apify") == "apify-key-0"

    def test_manual_rotation_advances_account(self, ledger):
        set_keys(ledger, "apify", 2)
        ledger.keyring.rotate("apify", "actor free-run limit")
        assert ledger.key_for("apify") == "apify-key-1"

    def test_exhausting_all_accounts_reports_unavailable(self, ledger):
        set_keys(ledger, "apify", 2)
        ledger.keyring.rotate("apify", "limit")
        ledger.keyring.rotate("apify", "limit")
        assert ledger.key_for("apify") == ""
        assert ledger.keyring.ring("apify").exhausted


class TestApifyPerActorCaps:
    """Free-run caps are per actor-developer. A capped FALLBACK actor must not
    retire an account that still has runs left for the PRIMARY actor."""

    def test_marker_list_excludes_informational_counters(self):
        from mp.sources.apify import _RUN_LIMIT_MARKERS
        informational = "apify free plan: run 1/1 of this actor, 1/15 runs and 0/35"
        assert not any(m in informational for m in _RUN_LIMIT_MARKERS)

    def test_marker_list_matches_real_cap_messages(self):
        from mp.sources.apify import _RUN_LIMIT_MARKERS
        for real in ("free users are limited to 10 runs. please upgrade to a paid plan to run more.",
                     "[status message]: free user run limit reached"):
            assert any(m in real for m in _RUN_LIMIT_MARKERS), real

    def test_cap_is_tracked_per_token_and_actor(self):
        from mp.sources import apify
        apify._capped_pairs.clear()
        apify._capped_pairs.add(("tok-a", apify.ACTOR_PEOPLE_SEARCH_FALLBACK))
        # The primary actor on the same account must still be considered usable.
        assert ("tok-a", apify.ACTOR_PEOPLE_SEARCH) not in apify.capped_pairs()
        apify._capped_pairs.clear()


class TestApifyCapMessageCoverage:
    """Every real cap phrasing observed in Apify logs must be detected.

    Missing one is expensive: 40 calls were dispatched against an actor whose
    monthly allowance was already exhausted, all returning empty datasets.
    """

    REAL_MESSAGES = [
        "free users are limited to 10 runs. please upgrade to a paid plan to run more.",
        "[status message]: free user run limit reached",
        "free users are limited up to 10 items per run. please upgrade to a paid plan",
        "apify free plan: this actor can be started 1 time(s) per calendar month per "
        "user, and that allowance is used up.",
    ]
    INFORMATIONAL = [
        "apify free plan: run 1/1 of this actor, 1/15 runs and 0/35",
        "starting to scrape query {...} with profile scraper mode 0",
        "done: 15 employee(s) returned",
    ]

    def test_all_real_cap_messages_detected(self):
        from mp.sources.apify import _RUN_LIMIT_MARKERS
        for msg in self.REAL_MESSAGES:
            assert any(m in msg for m in _RUN_LIMIT_MARKERS), msg

    def test_informational_lines_not_flagged(self):
        from mp.sources.apify import _RUN_LIMIT_MARKERS
        for msg in self.INFORMATIONAL:
            assert not any(m in msg for m in _RUN_LIMIT_MARKERS), msg


class TestCapCheckScoping:
    def test_actor_never_run_is_not_capped(self, monkeypatch):
        """An account that never ran an actor cannot have exhausted its allowance."""
        import requests

        import mp.sources.apify as ap

        class FakeResp:
            ok = True
            text = "free user run limit reached"

            @staticmethod
            def json():
                # Only a DIFFERENT actor appears in this account's run history.
                return {"data": {"items": [{"id": "r1", "actId": "OTHER_ACTOR",
                                            "statusMessage": None}]}}

        # `requests` is imported inside the function, so patch the library itself.
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp())
        assert ap._hit_actor_run_limit("tok", "TARGET_ACTOR") is False

    def test_matching_actor_run_with_cap_message_is_capped(self, monkeypatch):
        import requests

        import mp.sources.apify as ap

        class FakeResp:
            ok = True
            text = ("Apify Free plan: this Actor can be started 1 time(s) per calendar "
                    "month per user, and that allowance is used up.")

            @staticmethod
            def json():
                return {"data": {"items": [{"id": "r1", "actId": "TARGET_ACTOR",
                                            "statusMessage": None}]}}

        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp())
        assert ap._hit_actor_run_limit("tok", "TARGET_ACTOR") is True


class TestActorExhaustionIsolation:
    """One capped actor must not disable the whole provider.

    Regression: exhausting the primary people-search actor tripped Apify entirely,
    stranding 30 available runs on the fallback actor and the Instagram actor.
    """

    def test_provider_not_tripped_by_single_actor_cap(self, ledger):
        set_keys(ledger, "apify", 3)
        ring = ledger.keyring.ring("apify")
        # Simulate every account being capped for one specific actor.
        for _ in range(3):
            ring.rotate("primary actor capped")
        assert ring.exhausted
        # The ledger itself must remain untripped so other actors stay reachable.
        assert not ledger.summary().get("apify", {}).get("tripped", False)

    def test_ring_can_be_rewound_for_a_different_actor(self, ledger):
        set_keys(ledger, "apify", 3)
        ring = ledger.keyring.ring("apify")
        ring.rotate("x")
        ring.retired.clear()
        ring.index = 0
        assert ledger.key_for("apify") == "apify-key-0"


class TestActorCapDoesNotRetireAccounts:
    """An account capped for one actor must remain usable for every other actor.

    Regression: rotating on an actor cap retired all 5 accounts, so the fallback
    people-search actor (30 runs available) and the Instagram actor were unreachable.
    """

    def test_advance_moves_index_without_retiring(self, ledger):
        from mp.sources.apify import _advance

        class FakeHttp:
            def __init__(self, led): self.ledger = led

        set_keys(ledger, "apify", 5)
        ring = ledger.keyring.ring("apify")
        for _ in range(4):
            assert _advance(FakeHttp(ledger)) is True
        assert ring.retired == set(), "actor caps must not retire accounts"
        assert ring.available is True
        assert ledger.key_for("apify") == "apify-key-4"

    def test_ring_wraps_around(self, ledger):
        from mp.sources.apify import _advance

        class FakeHttp:
            def __init__(self, led): self.ledger = led

        set_keys(ledger, "apify", 3)
        for _ in range(3):
            _advance(FakeHttp(ledger))
        assert ledger.key_for("apify") == "apify-key-0"


class TestPerActorCosting:
    """A flat per-call estimate made the monthly guard fire ~30x too early and
    silently disabled Instagram verification. Costs must reflect the actor."""

    def test_instagram_is_cheaper_than_people_search(self, ledger):
        spec = ledger.spec("apify")
        by_actor = spec.get("cost_by_actor") or {}
        assert by_actor["apify/instagram-profile-scraper"] < by_actor[
            "harvestapi/linkedin-profile-search"]

    def test_actor_specific_price_is_applied(self, ledger):
        ledger.record("apify",
                      "https://api.apify.com/v2/acts/apify~instagram-profile-scraper/run",
                      status=200)
        assert ledger.total_cost() < 0.01, "should use the $0.0026 Instagram rate"

    def test_monthly_cap_is_a_safety_net_not_the_operative_limit(self, ledger):
        assert ledger.spec("apify")["per_month"] >= 10000


class TestStaleEmptyCacheRetry:
    """A cached EMPTY actor response must trigger one live retry, not a give-up.

    Regression: an empty dataset stored while an account was out of runs became
    permanent — the chunk was invalidated but never re-fetched, so 48 people stayed
    unverified across every sweep despite available capacity.
    """

    def test_source_retries_after_invalidating_cached_empty(self):
        import inspect

        from mp.sources import apify
        src = inspect.getsource(apify._run_actor)
        assert "was_cached" in src and "retried_live" in src, (
            "empty-from-cache must be distinguished from empty-from-live")
        assert src.index("http.invalidate") < src.index("retried_live = True"), (
            "cache entry must be dropped before the retry")


class TestFundedAccountSelection:
    """An account with actor allowance but NO budget returns empty datasets.

    Regression: sweeps started on account 0 ($0.00 left) and appeared to succeed
    while returning nothing, wasting whole runs and freezing verification.
    """

    def test_usable_requires_budget(self, monkeypatch):
        import mp.sources.apify as ap
        ap._capped_pairs.clear()
        monkeypatch.setattr(ap, "account_budget", lambda t, refresh=False:
                            {"broke": 0.0, "funded": 5.0}.get(t, 0.0))
        assert ap._usable("funded", "some/actor") is True
        assert ap._usable("broke", "some/actor") is False

    def test_capped_pair_is_never_usable_even_when_funded(self, monkeypatch):
        import mp.sources.apify as ap
        ap._capped_pairs.clear()
        monkeypatch.setattr(ap, "account_budget", lambda t, refresh=False: 9.0)
        ap._capped_pairs.add(("tok", "some/actor"))
        assert ap._usable("tok", "some/actor") is False
        ap._capped_pairs.clear()
