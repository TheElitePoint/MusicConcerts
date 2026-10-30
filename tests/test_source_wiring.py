"""Guard: every source declared `enabled` must have a real adapter behind it.

A config listing a source with no adapter is silently inert -- the pipeline reports
success while that source contributes nothing. This test makes that impossible.
"""
import importlib

import pytest

from mp import config

# Sources handled inline by a stage rather than by a sources/<name>.py module.
STAGE_LOCAL = {"website_team", "website_about", "company_instagram", "apify_linkedin",
               "cached", "website_links"}


def _adapter_exists(name: str) -> bool:
    if name in STAGE_LOCAL:
        return True
    try:
        importlib.import_module(f"mp.sources.{name}")
        return True
    except ModuleNotFoundError:
        return False


@pytest.mark.parametrize("stage", ["discovery", "resolution", "verification",
                                   "people", "profiles"])
def test_enabled_sources_have_adapters(stage):
    for name in config.sources()[stage].get("enabled", []):
        assert _adapter_exists(name), (
            f"{stage}.enabled lists '{name}' but mp/sources/{name}.py does not exist -- "
            "the source would be silently inert")


@pytest.mark.parametrize("stage", ["discovery", "resolution"])
def test_planned_sources_are_not_enabled(stage):
    cfg = config.sources()[stage]
    overlap = set(cfg.get("planned", [])) & set(cfg.get("enabled", []))
    assert not overlap, f"{stage}: {overlap} marked both planned and enabled"


class TestSearchBackendCircuitBreaker:
    """A throttled scraped backend must be abandoned, not retried on every query."""

    def setup_method(self):
        from mp.sources import websearch
        websearch.reset_backends()

    def test_backend_marked_dead_after_repeated_failures(self):
        from mp.sources import websearch
        for _ in range(3):
            websearch._note_backend_result("duckduckgo", False)
        assert "duckduckgo" in websearch.backend_status()["dead"]

    def test_success_resets_failure_count(self):
        from mp.sources import websearch
        websearch._note_backend_result("duckduckgo", False)
        websearch._note_backend_result("duckduckgo", False)
        websearch._note_backend_result("duckduckgo", True)
        websearch._note_backend_result("duckduckgo", False)
        assert "duckduckgo" not in websearch.backend_status()["dead"]

    def test_reset_clears_state(self):
        from mp.sources import websearch
        for _ in range(3):
            websearch._note_backend_result("searxng", False)
        websearch.reset_backends()
        assert websearch.backend_status()["dead"] == []


class TestCompanySlugMatching:
    """Regression: a 6-char prefix overlap matched unrelated companies.

    Real false positives observed: Kscope -> "kscopeai" (an AI company),
    "Colour and Pitch" -> "colour-and-stripe", "Home Normal" -> "normal-business".
    """

    def _match(self, name, slug):
        from mp.stages.s3_verify import _company_slug_matches
        return _company_slug_matches(name, slug)

    def test_exact_and_suffix_variants_accepted(self):
        assert self._match("Bella Union", "bella-union")
        assert self._match("Partisan Records", "partisan-records")
        assert self._match("Cr2 Records", "cr2-records-ltd")
        assert self._match("Napalm Records", "napalmrecords")

    def test_real_false_positives_rejected(self):
        assert not self._match("Kscope", "kscopeai")
        assert not self._match("Colour and Pitch", "colour-and-stripe")
        assert not self._match("Home Normal", "normal-business")
        assert not self._match("Dead Oceans", "oceansxyz")
        assert not self._match("FSOL Digital", "soldigitalasia")

    def test_empty_inputs(self):
        assert not self._match("", "anything")
        assert not self._match("Foo", "")


class TestEmployerMatchGuard:
    """SS34/SS52: people whose LinkedIn employer differs from the company we searched
    must be discarded, and the company URL treated as wrong.

    Regression: "Hear No Evil Recordings" (UK label) resolved to a LinkedIn page
    owned by an unrelated recording studio.
    """

    def test_employer_comparison_logic(self):
        from mp.normalize import normalize_company_name as n
        expected = n("Partisan Records")
        assert n("Partisan Records") in expected or expected in n("Partisan Records")
        # A different employer must not match.
        other = n("Hear No Evil Inc")
        assert not (expected in other or other in expected)

    def test_suffix_variants_still_match(self):
        from mp.normalize import normalize_company_name as n
        expected, found = n("Cr2 Records"), n("Cr2 Records Ltd")
        assert expected in found or found in expected


class TestCompanyNameDistinctiveness:
    """Name-based people search collides on generic company names.

    Regression: "Jo & Co" returned a gifting company's staff and "Rhino" matched
    Warner's catalogue label, putting 25 wrong people into the final sheet.
    """

    def _d(self, name):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path.cwd() / "scripts"))
        from parallel_people import name_is_distinctive
        return name_is_distinctive(name)

    def test_multiword_names_are_searchable(self):
        for n in ("Partisan Records", "Gondwana Records", "Rough Trade",
                  "Echo Beach", "The Flenser", "Cr2 Records"):
            assert self._d(n), n

    def test_single_common_word_names_are_skipped(self):
        for n in ("Rhino", "Civilians", "Mute", "Jo & Co"):
            assert not self._d(n), n

    def test_two_letter_fragments_are_not_distinctive(self):
        """'Jo & Co' is two words but both are fragments -- it still collides."""
        assert not self._d("Jo & Co")
