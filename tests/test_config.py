import pytest

"""The SOP config must stay faithful to the PDF. These assertions are the guardrail."""
from mp import config


class TestGeography:
    def test_33_approved_countries(self):
        assert len(config.approved_countries()) == 33

    def test_key_countries_present(self):
        c = config.approved_countries()
        for iso in ("US", "CA", "MX", "GB", "DE", "JP", "KR", "SG", "BR", "CL", "AU", "NZ"):
            assert iso in c

    def test_unapproved_markets_absent(self):
        """SS7: no Africa, no broad South/Southeast Asia."""
        c = config.approved_countries()
        for iso in ("NG", "ZA", "KE", "EG", "IN", "PK", "ID", "TH", "VN", "PH", "CN"):
            assert iso not in c


class TestScoringTotals:
    def test_sections_sum_to_90(self):
        assert sum(s["max"] for s in config.scoring()["sections"].values()) == 90

    def test_each_channel_adds_10(self):
        for ch in ("linkedin", "instagram"):
            block = config.scoring()["channel_readiness"][ch]
            assert sum(v["max"] for v in block.values() if isinstance(v, dict)) == 10

    def test_thresholds_match_sop45(self):
        t = config.scoring()["thresholds"]
        assert t["final_pass"] == 90 and t["research_further"] == 85

    def test_all_14_hard_gates(self):
        assert len(config.scoring()["hard_gates"]) == 14


class TestSheetColumns:
    def test_linkedin_columns_exact(self):
        cols = config.sop()["sheets"]["linkedin"]["columns"]
        assert len(cols) == 18
        assert cols[0] == "Company Name" and cols[-1] == "Verification Status"
        assert cols[13] == "Person LinkedIn URL"

    def test_instagram_columns_exact(self):
        cols = config.sop()["sheets"]["instagram"]["columns"]
        assert len(cols) == 19
        assert "Instagram Profile Status" in cols
        assert cols[13] == "Person Instagram URL"

    def test_mandatory_subset_of_columns(self):
        for ch in ("linkedin", "instagram"):
            block = config.sop()["sheets"][ch]
            assert set(block["mandatory"]) <= set(block["columns"])

    def test_channel_profile_is_mandatory_for_its_own_sheet(self):
        """SS44 hard gate: each sheet requires its own channel's personal profile."""
        assert "Person LinkedIn URL" in config.sop()["sheets"]["linkedin"]["mandatory"]
        assert "Person Instagram URL" in config.sop()["sheets"]["instagram"]["mandatory"]

    def test_cross_channel_profile_is_optional(self):
        """SS47/SS48: the *other* channel's profile is 'where available', not mandatory."""
        assert "Person Instagram URL" not in config.sop()["sheets"]["linkedin"]["mandatory"]
        assert "Person LinkedIn URL" not in config.sop()["sheets"]["instagram"]["mandatory"]


class TestExclusions:
    def test_all_11_excluded_brands(self):
        brands = config.sop()["existing_relationships"]["core_brands"]
        assert len(brands) == 11
        for b in ("Universal", "Sony", "Warner", "BMG", "Believe", "The Orchard"):
            assert b in brands

    def test_ten_hard_exclusion_groups(self):
        assert len(config.sop()["hard_exclusions"]) == 10

    def test_marketing_competitor_exclusion_present(self):
        """SS8.8 -- the exclusion that matters most, since we ARE the competitor."""
        kw = config.sop()["hard_exclusions"]["music_marketing_competitors"]["keywords"]
        assert "playlist promotion" in kw


class TestRoleFamilies:
    def test_seven_families(self):
        assert len(config.sop()["role_families"]) == 7

    def test_families_carry_seniority_and_relevance(self):
        tiers = config.sop()["seniority_tiers"]
        for name, fam in config.sop()["role_families"].items():
            assert fam["seniority"] in tiers, name
            assert 0 < fam["relevance"] <= 10, name

    def test_instagram_username_similarity_scores_zero(self):
        """SS35: a lookalike username alone must never verify a profile."""
        sig = config.sop()["instagram_identity_signals"]
        assert sig["username_resembles_name"] == 0
        assert sig["min_score_to_verify"] >= 5


class TestExporterFactCoverage:
    """Every mandatory sheet column must be backed by a key the scorer produces.

    Regression: `Activity Evidence URL` was read by the exporter but never set by
    build_facts, so all 17 FINAL PASS records were silently blocked at export.
    """

    FACT_KEY_FOR_COLUMN = {
        "Company Name": "company_name", "Company Website": "company_website",
        "Company Type": "company_type", "Country": "country",
        "Estimated Artist Roster": "roster_display",
        "Roster Evidence URL": "roster_evidence_url",
        "Activity Evidence URL": "activity_evidence_url",
        "Recent Activity / Intent Signal": "signal_categories",
        "Person Name": "person_name", "Current Job Title": "person_title",
        "Person LinkedIn URL": "linkedin_url", "Person Instagram URL": "instagram_url",
    }

    def test_build_facts_emits_every_mapped_key(self, tmp_path):
        from mp.db import Database, utcnow
        from mp.pipeline import Context
        from mp.stages.s8_score import build_facts

        ctx = Context.create(db_path=tmp_path / "t.sqlite", runs_root=tmp_path / "runs")
        try:
            ctx.db.x("""INSERT INTO companies (company_id, canonical_name,
                        name_normalized, status, first_seen)
                        VALUES (1,'Foo Records','foo records','QUALIFIED',?)""",
                     (utcnow(),))
            ctx.db.x("""INSERT INTO people (person_id, company_id, full_name,
                        name_normalized, title, status, first_seen)
                        VALUES (1,1,'Jane Doe','jane doe','Head of Digital',
                                'VERIFIED',?)""", (utcnow(),))
            facts = build_facts(ctx, 1)
            missing = [f"{col} -> {key}"
                       for col, key in self.FACT_KEY_FOR_COLUMN.items()
                       if key not in facts]
            assert not missing, f"build_facts omits keys backing columns: {missing}"
        finally:
            ctx.close()


class TestActivitySignalCategories:
    """SS41 B2 scores the number of DISTINCT SS27 categories present.

    Regression: only `release_activity` and `multi_artist_activity` were ever
    detected, capping B2 at 7/10 for every company and systematically holding
    otherwise-qualified people at 85-89.
    """

    def test_detector_covers_multiple_sop_categories(self):
        from mp.sources.website import _SIGNAL_PATTERNS
        sop_categories = set(config.sop()["activity_signal_categories"])
        detected = set(_SIGNAL_PATTERNS)
        assert detected <= sop_categories, "detector must only emit SS27 categories"
        assert len(detected) >= 5, "too few categories to lift B2 above the ceiling"

    def test_three_distinct_categories_are_reachable(self):
        """B2 awards 10 points at >=3 distinct categories."""
        from mp.sources.website import _SIGNAL_PATTERNS
        # release_activity + multi_artist_activity are derived elsewhere; the site
        # detector must be able to contribute at least one more on its own.
        assert len(_SIGNAL_PATTERNS) >= 1

    @pytest.mark.parametrize("category,text", [
        ("new_signing", "welcome to the label our latest signing"),
        ("hiring", "we are hiring a Head of Digital"),
        ("new_partnership", "partners with Bar Distribution"),
        ("new_distribution", "announced a distribution deal"),
        ("roster_expansion", "artists join our roster this month"),
        ("market_expansion", "opens a new office in Berlin"),
    ])
    def test_each_pattern_matches(self, category, text):
        from mp.sources.website import _SIGNAL_PATTERNS
        assert _SIGNAL_PATTERNS[category].search(text)
