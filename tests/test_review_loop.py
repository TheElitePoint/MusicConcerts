"""The review loop: human verdicts must be permanent and stop re-research."""
import csv

import pytest

from mp.db import Database, utcnow
from mp.pipeline import Context
from mp.stages import review_ingest


@pytest.fixture
def ctx(tmp_path):
    c = Context.create(db_path=tmp_path / "t.sqlite", runs_root=tmp_path / "runs")
    c.db.x("""INSERT INTO companies (company_id, canonical_name, name_normalized,
              status, first_seen) VALUES (1,'Foo Records','foo records','REVIEW',?)""",
           (utcnow(),))
    c.db.x("""INSERT INTO companies (company_id, canonical_name, name_normalized,
              status, first_seen) VALUES (2,'Bar Music','bar music','REVIEW',?)""",
           (utcnow(),))
    yield c
    c.close()


def write_csv(path, rows):
    cols = ["entity_type", "entity_id", "name", "company", "status",
            "missing_or_unclear", "how_to_resolve", "evidence_urls",
            "suggested_verdict", "suggested_reason", "verdict"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    return path


class TestReviewIngest:
    def test_human_reject_is_permanent(self, ctx, tmp_path):
        f = write_csv(tmp_path / "r.csv",
                      [{"entity_type": "company", "entity_id": 1, "verdict": "REJECT"}])
        assert review_ingest.run(ctx, f)["applied"] == 1

        assert ctx.db.one("SELECT status FROM companies WHERE company_id=1")["status"] == "REJECTED"
        skip, why = ctx.db.should_skip_company(1)
        assert skip, "a human REJECT must permanently stop re-research"

    def test_human_pass_qualifies(self, ctx, tmp_path):
        f = write_csv(tmp_path / "r.csv",
                      [{"entity_type": "company", "entity_id": 1, "verdict": "PASS"}])
        review_ingest.run(ctx, f)
        assert ctx.db.one("SELECT status FROM companies WHERE company_id=1")["status"] == "QUALIFIED"

    def test_blank_verdict_is_skipped(self, ctx, tmp_path):
        f = write_csv(tmp_path / "r.csv",
                      [{"entity_type": "company", "entity_id": 1, "verdict": ""}])
        stats = review_ingest.run(ctx, f)
        assert stats == {"applied": 0, "skipped_blank": 1, "invalid": 0}
        assert ctx.db.one("SELECT status FROM companies WHERE company_id=1")["status"] == "REVIEW"

    def test_groq_suggestion_alone_does_nothing(self, ctx, tmp_path):
        """Only the human `verdict` column acts; suggestions are advisory."""
        f = write_csv(tmp_path / "r.csv", [{"entity_type": "company", "entity_id": 1,
                                            "suggested_verdict": "REJECT", "verdict": ""}])
        review_ingest.run(ctx, f)
        assert ctx.db.one("SELECT status FROM companies WHERE company_id=1")["status"] == "REVIEW"

    def test_invalid_verdict_rejected(self, ctx, tmp_path):
        f = write_csv(tmp_path / "r.csv",
                      [{"entity_type": "company", "entity_id": 1, "verdict": "MAYBE"}])
        assert review_ingest.run(ctx, f)["invalid"] == 1

    def test_verdict_is_recorded_for_audit(self, ctx, tmp_path):
        f = write_csv(tmp_path / "r.csv",
                      [{"entity_type": "company", "entity_id": 2, "verdict": "REJECT"}])
        review_ingest.run(ctx, f)
        row = ctx.db.one("""SELECT verdict, decided_by FROM review_verdicts
                            WHERE entity_type='company' AND entity_id=2""")
        assert row["verdict"] == "REJECT" and row["decided_by"] == "human"

    def test_missing_file_raises(self, ctx):
        with pytest.raises(FileNotFoundError):
            review_ingest.run(ctx, "/nonexistent/review.csv")


class TestSkipIndex:
    """The skip index is what prevents wasted scraping and API calls."""

    def test_recently_verified_company_is_skipped(self, ctx):
        ctx.db.x("UPDATE companies SET status='VERIFIED', last_researched=? WHERE company_id=1",
                 (utcnow(),))
        assert ctx.db.should_skip_company(1)[0]

    def test_alias_lookup_finds_company_before_network(self, ctx):
        ctx.db.add_alias("foo records", 1, "Foo Records", "musicbrainz")
        assert ctx.db.alias_lookup("foo records") == 1
        assert ctx.db.alias_lookup("unknown label") is None


class TestVerificationDoesNotRegress:
    """A transient inability to re-fetch must not undo an existing verdict.

    Regression: each loop cycle re-scored Instagram identity from empty bio/links
    whenever the profile fetch was skipped, downgrading VERIFIED profiles and
    taking Instagram Final from 3 rows back to 0 repeatedly.
    """

    def test_identity_score_needs_real_signals(self):
        from mp.stages.s7_person_verify import instagram_identity_score
        empty, _ = instagram_identity_score(
            bio="", full_name="", person="Jane Doe", company="Foo Records",
            external_links=[], company_ig="", roster_artists=[])
        rich, _ = instagram_identity_score(
            bio="Head of Digital at Foo Records", full_name="Jane Doe",
            person="Jane Doe", company="Foo Records",
            external_links=["https://foorecords.com"],
            company_ig="https://instagram.com/foorecords", roster_artists=[])
        assert empty == 0 and rich >= 5, (
            "empty inputs must score 0 -- which is why they must never be used to "
            "re-evaluate an already verified profile")


class TestInstagramVerdictStability:
    """A failed/skipped profile fetch must not change the stored verdict either way.

    Regression: protecting only the VERIFIED case still allowed a profile to flip to
    UNVERIFIED once and remain there, so scoring caught it in a downgraded window and
    Instagram Final kept collapsing to 0.
    """

    def test_empty_fetch_yields_zero_score(self):
        from mp.stages.s7_person_verify import instagram_identity_score
        score, _ = instagram_identity_score(
            bio="", full_name="", person="Jane Doe", company="Foo Records",
            external_links=[], company_ig="", roster_artists=[])
        assert score == 0

    def test_source_guard_present(self):
        """The skip must be unconditional on fetch failure, not status-dependent."""
        import inspect

        from mp.stages import s7_person_verify
        src = inspect.getsource(s7_person_verify.run)
        assert "if not fetched:" in src, (
            "fetch failure must short-circuit BEFORE re-scoring identity")


class TestEmptyInstagramPayload:
    """A returned-but-empty profile must not be treated as usable evidence.

    Regression: the actor returns a record whose bio/name/links are all blank
    (private or partially scraped). `found` was True, so identity re-scored to 0 and
    verified profiles flipped to UNVERIFIED, collapsing Instagram Final repeatedly.
    """

    def test_signal_fields_required_for_scoring(self):
        from mp.sources.apify import ProfileResult
        empty = ProfileResult(url="https://instagram.com/x", handle="x")
        assert empty.found is True, "found only means a record came back"
        usable = bool(empty.found and (empty.bio or empty.full_name
                                       or empty.external_links))
        assert usable is False, "no signal fields means nothing to score"

    def test_populated_payload_is_usable(self):
        from mp.sources.apify import ProfileResult
        real = ProfileResult(url="https://instagram.com/x", handle="x",
                             bio="Head of Digital at Foo Records")
        assert bool(real.found and (real.bio or real.full_name
                                    or real.external_links)) is True


class TestExportExcludesRejected:
    """A FINAL PASS score row outlives a later rejection.

    Regression: a person purged as an SS50 parsing failure still appeared in
    LinkedIn Final at score 91, because export joined scores->people without
    re-checking status.
    """

    def test_export_query_filters_status(self):
        import inspect

        from mp.stages import s9_export
        src = inspect.getsource(s9_export.run)
        assert "p.status != 'REJECTED'" in src
        assert "c.status = 'QUALIFIED'" in src
