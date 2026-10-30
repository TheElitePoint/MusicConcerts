"""SS52 duplicate control and URL standards (SS53)."""
import pytest

from mp.normalize import (canonical_url, company_dedupe_keys, instagram_handle,
                          instagram_url, is_person_linkedin, linkedin_slug,
                          loose_company_key, normalize_company_name,
                          normalize_person_name, person_dedupe_keys,
                          registrable_domain)


class TestCompanyNames:
    def test_legal_suffixes_stripped(self):
        assert normalize_company_name("Example Records Ltd") == "example records"
        assert normalize_company_name("Example Records GmbH") == "example records"

    def test_sop52_variants_share_loose_key(self):
        """SS52: these three 'may represent the same company'."""
        keys = {loose_company_key(n) for n in
                ("Example Records", "Example Records Ltd", "Example Music Records")}
        assert len(keys) == 1

    def test_loose_key_does_not_collapse_distinct_companies(self):
        assert loose_company_key("Foo Records") != loose_company_key("Bar Records")

    def test_strict_key_keeps_descriptors(self):
        """Strict key must NOT merge a label with a management arm -- auto-merge safety."""
        assert normalize_company_name("Foo Records") != normalize_company_name("Foo Management")

    def test_accents_and_ampersand(self):
        # Combining accents fold away; "&" normalizes to "and".
        assert normalize_company_name("Bj\u00f6rk & S\u00f6ns") == "bjork and sons"
        assert normalize_company_name("Caf\u00e9 R\u00e9cords") == "cafe records"

    def test_empty(self):
        assert normalize_company_name("") == ""
        assert loose_company_key("") == ""


class TestDomains:
    @pytest.mark.parametrize("value,expected", [
        ("https://www.sub.example.co.uk/roster", "example.co.uk"),
        ("http://example.com", "example.com"),
        ("example.com/path", "example.com"),
        ("https://EXAMPLE.COM", "example.com"),
        ("not a url", ""),
        ("", ""),
    ])
    def test_registrable_domain(self, value, expected):
        assert registrable_domain(value) == expected

    def test_canonical_url_strips_tracking(self):
        assert (canonical_url("https://www.example.com/roster/?utm_source=x#top")
                == "https://example.com/roster")


class TestInstagram:
    @pytest.mark.parametrize("value,expected", [
        ("https://instagram.com/Foo.Bar_1/", "foo.bar_1"),
        ("@FooBar", "foobar"),
        ("https://www.instagram.com/foobar?hl=en", "foobar"),
        ("https://www.instagram.com/p/Cxyz123/", ""),      # a post, not a profile
        ("https://www.instagram.com/reel/Cabc/", ""),
        ("", ""),
    ])
    def test_handle_extraction(self, value, expected):
        assert instagram_handle(value) == expected

    def test_url_roundtrip(self):
        assert instagram_url("@FooBar") == "https://www.instagram.com/foobar/"
        assert instagram_url("") == ""


class TestLinkedIn:
    def test_person_vs_company(self):
        assert linkedin_slug("https://www.linkedin.com/in/sarah-williams-123/") == (
            "in", "sarah-williams-123")
        assert linkedin_slug("https://linkedin.com/company/example-records") == (
            "company", "example-records")

    def test_sop53_company_page_is_not_a_person(self):
        """SS53 forbids submitting a company page where a person profile is required."""
        assert is_person_linkedin("https://linkedin.com/company/example-records") is False
        assert is_person_linkedin("https://linkedin.com/in/sarah-williams") is True


class TestDedupeKeys:
    def test_company_key_order_is_sop52(self):
        keys = company_dedupe_keys(website="https://example.com", instagram="@ex_rec",
                                   linkedin="linkedin.com/company/exrec",
                                   name="Example Records Ltd")
        assert [k for k, _ in keys] == ["domain", "instagram", "linkedin", "name"]

    def test_company_keys_skip_missing(self):
        keys = company_dedupe_keys(name="Example Records")
        assert keys == [("name", "example records")]

    def test_person_key_order_is_sop52(self):
        keys = person_dedupe_keys(linkedin="linkedin.com/in/sw", instagram="@sw",
                                  name="Sarah Williams", company_id=7,
                                  title="Head of Digital")
        assert [k for k, _ in keys] == ["linkedin", "instagram", "name_company", "name_title"]

    def test_person_company_page_not_used_as_person_key(self):
        keys = person_dedupe_keys(linkedin="linkedin.com/company/foo", name="Sarah Williams",
                                  company_id=1)
        assert not any(k == "linkedin" for k, _ in keys)

    def test_person_name_honorifics(self):
        assert normalize_person_name("Dr. Sarah Williams, PhD") == "sarah williams"


class TestLinkedInUrnCasing:
    """LinkedIn member URNs are case-sensitive; lowercasing one yields a dead link.

    Regression: every Apify-sourced profile URL was being lowercased, so all 17
    FINAL PASS rows carried unusable person URLs.
    """

    URN = "ACwAAAQ0LdIB6OF3pedJZl5EZ8aQHgpojoR532M"

    def test_urn_case_preserved(self):
        kind, slug = linkedin_slug(f"https://www.linkedin.com/in/{self.URN}")
        assert kind == "in" and slug == self.URN

    def test_person_url_round_trips_exactly(self):
        from mp.normalize import linkedin_person_url
        url = f"https://www.linkedin.com/in/{self.URN}"
        assert linkedin_person_url(url) == url

    def test_vanity_slug_unchanged(self):
        assert linkedin_slug("https://www.linkedin.com/in/sarah-williams-123/") == (
            "in", "sarah-williams-123")

    def test_company_slug_still_lowercased(self):
        assert linkedin_slug("https://linkedin.com/company/Bella-Union") == (
            "company", "bella-union")

    def test_dedupe_key_is_case_insensitive(self):
        from mp.normalize import person_dedupe_keys
        a = person_dedupe_keys(linkedin=f"https://linkedin.com/in/{self.URN}", name="A B")
        b = person_dedupe_keys(linkedin=f"https://linkedin.com/in/{self.URN.lower()}",
                               name="A B")
        assert a[0] == b[0]


class TestUrnVsVanityUrlMatching:
    """Stored profile URLs are often member URNs while scrapers return vanity URLs.

    Regression: matching fetched profiles by URL alone confirmed nobody, because
    /in/ACwAAAQ0LdIB... never equals /in/zena-white. Name matching bridges the two.
    """

    def test_urn_and_vanity_are_different_keys(self):
        from mp.normalize import linkedin_person_url
        urn = linkedin_person_url("https://www.linkedin.com/in/ACwAAAQ0LdIB6OF3")
        vanity = linkedin_person_url("https://www.linkedin.com/in/zena-white")
        assert urn != vanity

    def test_person_name_normalizes_across_sources(self):
        from mp.normalize import normalize_person_name
        assert (normalize_person_name("Conor  Mackie")
                == normalize_person_name("conor mackie"))
