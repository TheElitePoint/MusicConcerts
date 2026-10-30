"""SS30 role families / equivalent titles and SS31 person exclusions."""
import pytest

from mp.sop.roles import check_person_exclusion, classify_title


class TestEquivalentTitles:
    """SS30: 'Do not reject a strong person only because the exact wording differs.'"""

    def test_sop30_worked_example(self):
        """The SOP's own example: 'Director, Digital Strategy' ~ 'Head of Digital'."""
        listed = classify_title("Head of Digital")
        variant = classify_title("Director, Digital Strategy")
        assert variant.family == listed.family == "digital_marketing"
        assert variant.relevance == listed.relevance

    @pytest.mark.parametrize("title,family", [
        ("Global Head of Audience Development", "digital_marketing"),
        ("VP, Streaming Strategy", "digital_marketing"),
        ("Artist Marketing Director", "digital_marketing"),
        ("Head of Campaigns", "label_release"),
        ("Senior Label Manager", "label_release"),
        ("Director of A&R", "anr_artist_development"),
        ("Head of Artist Services", "artist_label_services"),
        ("Commercial Director", "artist_label_services"),
        ("Head of Operations", "operations"),
        ("COO", "operations"),
    ])
    def test_families_resolve(self, title, family):
        assert classify_title(title).family == family

    @pytest.mark.parametrize("title", ["Founder", "Co-Founder", "CEO", "President",
                                       "Owner", "Managing Director", "Managing Partner",
                                       "General Manager", "Co-Founder & CEO"])
    def test_leadership_without_function_keyword(self, title):
        m = classify_title(title)
        assert m.family == "leadership" and m.seniority == "executive"

    def test_unrelated_title_does_not_match(self):
        assert classify_title("Warehouse Driver").family == ""
        assert classify_title("").family == ""


class TestPersonExclusions:
    """SS31 -- 'Do not include a weak person simply because the company is strong.'"""

    @pytest.mark.parametrize("title", [
        "Intern", "Executive Assistant", "A&R Coordinator", "Marketing Assistant",
        "Social Media Coordinator", "Graphic Designer", "Photographer",
        "Audio Engineer", "Studio Manager", "Finance Director", "HR Manager",
    ])
    def test_excluded_titles(self, title):
        assert classify_title(title).excluded
        assert check_person_exclusion(title)[0]

    def test_sop59_finance_director_cannot_qualify(self):
        """SS59: 'A good company cannot compensate for an irrelevant person.'"""
        m = classify_title("Finance Director")
        assert m.excluded and not m.qualifies

    def test_leadership_overrides_exclusion_keyword(self):
        """A Founder who is also credited as a producer is still a decision-maker."""
        m = classify_title("Founder & Producer")
        assert m.family == "leadership" and not m.excluded

    def test_plain_producer_still_excluded(self):
        assert classify_title("Music Producer").excluded

    @pytest.mark.parametrize("title", [
        "Executive Assistant to Co-President",
        "EA to CEO / Label Assistant",
        "Assistant to Founder and Head of Marketing",
    ])
    def test_assistant_does_not_inherit_supported_executive_seniority(self, title):
        m = classify_title(title, small_company=True)
        assert m.excluded and not m.qualifies


class TestSeniorityWeighting:
    def test_sop30_2_small_company_note(self):
        """SS30.2: a Digital Marketing Manager may qualify in a SMALLER organization."""
        big = classify_title("Digital Marketing Manager")
        small = classify_title("Digital Marketing Manager", small_company=True)
        assert small.relevance > big.relevance
        assert small.relevance == 10

    def test_junior_titles_lose_relevance(self):
        assert classify_title("Junior Digital Associate").relevance < 5

    def test_seniority_tiers_assigned(self):
        assert classify_title("CEO").seniority == "executive"
        assert classify_title("Head of Digital").seniority == "senior"
        assert classify_title("Senior Artist Manager").seniority == "mid_senior"


class TestNonEnglishTitles:
    """SS30: 'Focus on actual function.' SS7 approves DE/AT/CH, FR, ES, IT, NL and
    the Nordics, where LinkedIn titles are usually in the local language."""

    @pytest.mark.parametrize("title", [
        "Geschäftsführer", "Inhaber", "Gründer",           # German
        "Directeur Général", "Fondateur",                   # French
        "Director General", "Fundador",                     # Spanish
        "Amministratore Delegato", "Fondatore",             # Italian
        "Oprichter", "Eigenaar",                            # Dutch
    ])
    def test_local_language_leadership_recognised(self, title):
        m = classify_title(title)
        assert m.qualifies and m.seniority == "executive", title

    def test_regression_napalm_managing_director(self):
        """Found in a real run: Napalm Records' most senior person was being missed."""
        assert classify_title("Geschäftsführer").family == "leadership"


class TestJuniorExclusion:
    @pytest.mark.parametrize("title", [
        "Junior Online Marketing Managerin", "Junior Digital Coordinator",
        "Trainee Marketing", "Intern, A&R",
    ])
    def test_junior_roles_excluded_regardless_of_language(self, title):
        assert classify_title(title, small_company=True).excluded

    def test_senior_manager_still_qualifies_at_small_company(self):
        m = classify_title("Digital Marketing Manager", small_company=True)
        assert m.qualifies and m.relevance == 10


class TestLinkedInSerpParsing:
    """Free people discovery parses LinkedIn search-result titles (SS33)."""

    def _p(self, title, company):
        from mp.stages.s5_people import _parse_linkedin_serp
        return _parse_linkedin_serp(title, company)

    def test_name_title_employer_form(self):
        assert self._p("Emily Moxon - Managing Director - Brownswood Recordings | LinkedIn",
                       "Brownswood Recordings") == ("Emily Moxon", "Managing Director")

    def test_internal_hyphen_preserved(self):
        """'Co-Founder' must not be split on its own hyphen."""
        assert self._p("Ollie Jacob - Co-Founder - Memphis Industries ltd",
                       "Memphis Industries") == ("Ollie Jacob", "Co-Founder")

    def test_at_employer_stripped(self):
        assert self._p("Daniel Yocom - Product Manager at Partisan Records | LinkedIn",
                       "Partisan Records") == ("Daniel Yocom", "Product Manager")

    def test_employer_only_yields_no_title(self):
        name, job = self._p("Zena White - Partisan Records | LinkedIn", "Partisan Records")
        assert name == "Zena White" and job == ""

    def test_non_person_result_rejected(self):
        assert self._p('20+ "Emily Moxon" profiles', "Brownswood") == ("", "")


class TestPeopleCapPerCompany:
    """SS51: the number of people depends on the company. 4-10 by roster band."""

    def test_bands(self):
        from mp.stages.s5_people import people_cap
        assert people_cap(5) == 8 and people_cap(9) == 8
        assert people_cap(10) == 12 and people_cap(20) == 12
        assert people_cap(25) == 15 and people_cap(30) == 15
        assert people_cap(45) == 18

    def test_unknown_roster_gets_max_ceiling(self):
        from mp.stages.s5_people import people_cap
        assert people_cap(None) == 18

    def test_cap_never_below_four(self):
        from mp.stages.s5_people import people_cap
        assert min(people_cap(r) for r in (1, 3, 5, 9, 20, 50)) >= 8


class TestBandCapProtectsPassers:
    """SS51: 'Do not remove genuinely strong people simply because several people
    already qualify.' A FINAL PASS record must never be trimmed by the band cap.

    Regression: the cap trimmed an already-passing Partisan Records person, taking
    the sheet backwards from 30 rows to 27.
    """

    def test_passing_records_are_ranked_first_and_kept(self, tmp_path):
        from mp.db import utcnow
        from mp.pipeline import Context
        from mp.stages.s5_people import _trim_to_cap

        ctx = Context.create(db_path=tmp_path / "t.sqlite", runs_root=tmp_path / "r")
        try:
            ctx.db.x("""INSERT INTO companies (company_id, canonical_name,
                        name_normalized, status, first_seen)
                        VALUES (1,'Foo','foo','QUALIFIED',?)""", (utcnow(),))
            # 6 people at a 5-artist company (cap 4). The two lowest-relevance
            # people are the ones that already PASSED.
            for i in range(6):
                ctx.db.x("""INSERT INTO people (person_id, company_id, full_name,
                            name_normalized, title, role_relevance, seniority,
                            status, first_seen)
                            VALUES (?,1,?,?,'Head of Digital',?,?,'CANDIDATE',?)""",
                         (i + 1, f"P{i}", f"p{i}", 10 - i,
                          "senior", utcnow()))
            for pid in (5, 6):
                ctx.db.x("""INSERT INTO scores (person_id, channel, subscores, total,
                            verdict, sop_version, scored_at)
                            VALUES (?,'linkedin','{}',95,'FINAL PASS','1.0',?)""",
                         (pid, utcnow()))
            _trim_to_cap(ctx, 1, roster=5)
            survivors = {r["person_id"] for r in ctx.db.q(
                "SELECT person_id FROM people WHERE company_id=1 AND status!='REJECTED'")}
            assert {5, 6} <= survivors, "passing records must never be trimmed"
        finally:
            ctx.close()


class TestSerpParserRejectsMalformed:
    """SS50 requires exactly one person per row.

    Regression: search snippets that merged two people ("...UK A&R\\n\\nJeannette Lee")
    or whose name was a company/role fragment ("King Co", "President Shannon Mann")
    were stored as real decision-makers and reached the final sheet.
    """

    def _p(self, title, company="Brownswood Recordings"):
        from mp.stages.s5_people import _parse_linkedin_serp
        return _parse_linkedin_serp(title, company)

    def test_multi_person_block_rejected(self):
        assert self._p("Geoff Travis - Senior Director, UK A&R\n\nJeannette Lee") == ("", "")

    def test_company_like_name_rejected(self):
        assert self._p("King Co - Founder & Managing Director") == ("", "")
        assert self._p("Grant Lee Co - Head of Digital") == ("", "")

    def test_role_fragment_as_name_rejected(self):
        assert self._p("President Shannon Mann - Director of Operations") == ("", "")

    def test_genuine_person_still_parses(self):
        assert self._p("Emily Moxon - Managing Director - Brownswood Recordings") == (
            "Emily Moxon", "Managing Director")
