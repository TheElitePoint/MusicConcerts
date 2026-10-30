"""SS8/SS9 exclusions, driven by the SOP's own worked examples (SS61-SS64)."""
import pytest

from mp.sop.exclusions import (check_excluded_brand, check_hard_exclusion,
                               is_approved_country)


class TestExistingRelationship:
    """SS9 + SS63 + SS64."""

    @pytest.mark.parametrize("name", [
        "Sony Music Mexico", "Sony Music US", "Sony Music Spain",
        "Universal Music Mexico", "Universal Music UK", "Warner Music France",
    ])
    def test_regional_variations_excluded(self, name):
        """SS9/SS63: changing the country does not make the core brand eligible."""
        assert check_excluded_brand(name).excluded

    @pytest.mark.parametrize("brand", [
        "Universal", "Sony", "Warner", "BMG", "Altafonte", "ONErpm", "Believe",
        "The Orchard", "Dromedario Records", "Cultura Rock", "Deep Delay",
    ])
    def test_every_core_brand_excluded(self, brand):
        assert check_excluded_brand(brand).excluded

    def test_sop64_separately_named_affiliate_not_auto_rejected(self):
        """SS64: ownership alone must NOT reject a separately named company."""
        r = check_excluded_brand("Heavenly Recordings", parent="Universal Music Group")
        assert r.outcome == "CLEAR"
        assert "research independently" in r.reason

    def test_ambiguous_prefix_goes_to_review_not_reject(self):
        """SS55 forbids guessing: 'Make Believe Records' is neither auto-passed nor killed."""
        assert check_excluded_brand("Make Believe Records").outcome == "REVIEW"

    def test_unrelated_company_is_clear(self):
        assert check_excluded_brand("Earache Records").outcome == "CLEAR"
        assert check_excluded_brand("Heavenly Recordings").outcome == "CLEAR"

    def test_empty_name_is_clear(self):
        assert check_excluded_brand("").outcome == "CLEAR"


class TestHardExclusions:
    def test_sop61_music_marketing_competitor(self):
        """SS61: 'Being music-related does not automatically make a company eligible.'"""
        r = check_hard_exclusion("Boost Agency",
                                 "playlist promotion, paid campaigns, influencer marketing")
        assert r.excluded and r.group == "music_marketing_competitors"

    def test_sop62_recording_studio(self):
        """SS62: artist credits do not make a studio a label."""
        r = check_hard_exclusion("Abbey Road Studios",
                                 "recording studio and mastering studio, many artists as clients")
        assert r.excluded and r.group == "studios_production"

    @pytest.mark.parametrize("name,desc,group", [
        ("BIMM", "a music school and academy", "music_education"),
        ("DICE", "", "ticketing"),
        ("Pitchfork", "an online magazine covering music", "media"),
        ("Perfect Day Events", "we plan weddings and corporate events", "private_corporate_events"),
        ("BrandCo", "a creative agency and branding agency", "creative_media_agencies"),
        ("PlayCo", "a gaming studio", "unrelated_entertainment"),
        ("LedgerCo", "an accounting firm", "unrelated_industries"),
    ])
    def test_each_exclusion_group(self, name, desc, group):
        r = check_hard_exclusion(name, desc)
        assert r.excluded and r.group == group

    def test_genuine_label_is_clear(self):
        r = check_hard_exclusion("Heavenly Recordings",
                                 "an independent record label with a roster of artists")
        assert r.outcome == "CLEAR"

    def test_no_substring_false_positives(self):
        """'radio station' must not fire on 'Radiohead'."""
        assert not check_hard_exclusion("Radiohead Records", "independent label").excluded

    def test_management_company_with_marketing_staff_not_a_competitor(self):
        """SS8.8: 'The primary business model matters.'"""
        r = check_hard_exclusion(
            "Example Artist Management",
            "a multi-artist management company representing 12 artists")
        assert r.outcome == "CLEAR"

    def test_empty_input_is_clear(self):
        assert check_hard_exclusion("", "", "").outcome == "CLEAR"


class TestGeography:
    @pytest.mark.parametrize("iso", ["US", "CA", "MX", "GB", "IE", "DE", "NO", "SE",
                                     "JP", "KR", "SG", "AU", "NZ", "BR", "CL"])
    def test_approved(self, iso):
        assert is_approved_country(iso)

    def test_norway_not_lost_to_yaml_boolean_coercion(self):
        """Regression: YAML 1.1 parses a bare NO as False, silently dropping Norway."""
        assert is_approved_country("NO")

    @pytest.mark.parametrize("iso", ["NG", "ZA", "KE", "IN", "PK", "ID", "TH", "CN", "RU"])
    def test_not_approved(self, iso):
        assert not is_approved_country(iso)

    def test_empty_is_not_approved(self):
        assert not is_approved_country("")


class TestIdentityCarriers:
    """SS9: a major-owned imprint is caught by its domain/socials, not just its name.

    Regression from a real run: "Mercury Records" cleared the gate because its name
    is not on the exclusion list and Wikidata reported no parent -- while its site
    was islanddefjam.com and its Instagram was /universalmusicgroup.
    """

    def test_universal_instagram_excludes(self):
        r = check_excluded_brand("Mercury Records",
                                 instagram="https://www.instagram.com/universalmusicgroup/")
        assert r.excluded and "Universal" == r.matched

    def test_sony_domain_excludes(self):
        r = check_excluded_brand("Some Imprint", domain="sonymusic.co.uk")
        assert r.excluded

    def test_warner_linkedin_excludes(self):
        r = check_excluded_brand("Some Imprint",
                                 linkedin="https://linkedin.com/company/warner-music-group")
        assert r.excluded

    def test_independent_identifiers_stay_clear(self):
        r = check_excluded_brand("Heavenly Recordings",
                                 domain="heavenlyrecordings.com",
                                 instagram="https://www.instagram.com/heavenlyjukebox/")
        assert r.outcome == "CLEAR"

    def test_short_brand_tokens_do_not_false_positive(self):
        """'BMG' is short; it must not match arbitrary substrings in a domain."""
        r = check_excluded_brand("Bmgabber Records", domain="bmgabberrecords.com")
        assert r.outcome in ("CLEAR", "REVIEW")
