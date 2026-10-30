"""Free-source enrichment: geography extraction, MB type mapping, attribution audit.

These cover the code that converts the REVIEW backlog. Each test pins a behaviour
that was wrong in production and cost real rows.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mp import config
from mp.sources.musicbrainz import MB_TYPE_TO_SOP
from mp.sources.website import extract_geo_signals

from audit_attributions import conflicts, core_name, employer_matches, name_is_company
from enrich_backlog import is_junk_domain


class TestGeoSignals:
    """SS7 permits geography that is established; a registration number is evidence."""

    def test_uk_company_registration(self):
        assert extract_geo_signals("Registered in England and Wales No. 12345678")["GB"] >= 2

    def test_german_impressum_markers(self):
        assert extract_geo_signals("Handelsregister Amtsgericht Berlin HRB 12345")["DE"] >= 2

    def test_dialling_code(self):
        assert extract_geo_signals("Call us on +44 (0)20 7946 0958")["GB"] >= 2
        assert extract_geo_signals("Tel: +81 3 1234 5678")["JP"] >= 2

    def test_uk_postcode_shape(self):
        assert extract_geo_signals("Unit 4, 12 Example St, London EC1V 9BX")["GB"] >= 2

    def test_unrelated_copy_yields_nothing(self):
        """Tour dates and prose must not vote -- that is why body text is excluded."""
        assert extract_geo_signals("Our artists have toured Germany, Japan and Brazil.") == {}

    def test_case_insensitive(self):
        assert extract_geo_signals("REGISTERED IN ENGLAND")["GB"] >= 2


class TestMusicBrainzTypeMapping:
    def test_every_target_is_a_real_sop_category(self):
        """A category invented to make G2 pass would be exactly what SS4 forbids."""
        valid = set(config.sop()["company_types"])
        assert set(MB_TYPE_TO_SOP.values()) <= valid

    def test_publisher_is_not_mapped(self):
        """Music publishing is not one of the five SS4 approved categories."""
        assert "Publisher" not in MB_TYPE_TO_SOP

    def test_production_types_map_to_label(self):
        assert MB_TYPE_TO_SOP["Original Production"] == "record_label"
        assert MB_TYPE_TO_SOP["Distributor"] == "music_distributor"


class TestJunkDomain:
    @pytest.mark.parametrize("domain", [
        "linkedin.com", "fontanaca.gov", "service.gov.uk", "music.apple.com", "x.com",
    ])
    def test_directory_and_social_hosts_are_junk(self, domain):
        assert is_junk_domain(domain)

    @pytest.mark.parametrize("domain", [
        "square-enix.com", "wolfspellrex.com", "theflenser.com", "notx.com",
    ])
    def test_real_company_domains_survive(self, domain):
        """A bare substring test read "square-enix.com" as the social host "x.com"
        and purged two real company sites."""
        assert not is_junk_domain(domain)


class TestEmployerMatching:
    def test_generic_suffix_does_not_break_identity(self):
        """"Earache" and "Earache Records" are the same employer."""
        assert employer_matches("Earache Records", "Earache")
        assert employer_matches("Epitaph", "Epitaph Records")

    def test_distinct_companies_never_match(self):
        assert not employer_matches("Earache Records", "Atlantic Records")
        assert not employer_matches("Bloodshot Records", "Western Publicity")

    def test_core_name_strips_only_generic_words(self):
        assert core_name("Partisan Records Ltd") == "partisan"
        # A name made only of generic words must not collapse to nothing.
        assert core_name("The Music Group") != ""


class TestHeadlineConflicts:
    def test_department_is_not_an_employer(self):
        """"of X" introduces a department far more often than a company; treating it
        as an employer produced 45 false positives against 3 real ones."""
        assert conflicts("Head of Marketing", "Partisan Records") == []
        assert conflicts("Director of A&R", "Dead Oceans") == []

    def test_real_conflict_is_caught(self):
        assert conflicts("Owner @ Western Publicity", "Bloodshot Records")
        assert conflicts("Digital Marketing Manager at Atlantic Records",
                         "Earache Records")

    def test_same_company_is_not_a_conflict(self):
        assert conflicts("Managing Director at Brownswood Music Ltd",
                         "Brownswood Recordings") == []


class TestNameIsCompany:
    def test_company_name_parsed_as_person(self):
        assert name_is_company("Chess Club", "Chess Club Records")
        assert name_is_company("Fabtone Fashion", "FABTONE")

    def test_founder_sharing_the_label_name_survives(self):
        """SS50 removes non-people, not founders who named the label after themselves."""
        assert not name_is_company("Shigeru Urashima", "Urashima")
        assert not name_is_company("Dunya Cengiz", "Dunya")

    def test_ordinary_person_unaffected(self):
        assert not name_is_company("Emily Moxon", "Brownswood Recordings")
