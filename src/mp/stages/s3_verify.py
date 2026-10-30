"""S3 -- Company verification (SS24, SS6, SS7, SS37, SS65 Stage 3).

Gathers the evidence the gates and scorer need, entirely from free sources:
  website (strongest, SS38) + Wikidata + MusicBrainz + iTunes cross-confirmation.

SS7 requires geography to be established rather than assumed, so country is decided
by VOTE across independent sources -- the website, Wikidata and MusicBrainz - and is
only marked "verified" when two of them agree.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta

from ..db import utcnow
from ..models import RosterEstimate
from ..normalize import instagram_url, linkedin_slug, normalize_company_name
from ..pipeline import Context
from ..sop.classify import classify
from ..sources import discogs, itunes, musicbrainz, website, websearch, wikidata

# Company SOP 01 v4.0 counts activity over a 12-month window (SS4.2); the old
# 180-day window was internal and held ~370 otherwise-qualified companies in REVIEW.
RECENT_DAYS = 365


def _resolve_country(site_iso: str, site_conf: str, wd_iso: str,
                     mb_iso: str) -> tuple[str, str]:
    """SS7: require two agreeing independent signals for 'verified'."""
    votes = Counter(iso for iso in (site_iso, wd_iso, mb_iso) if iso)
    if not votes:
        return "", "unknown"
    iso, count = votes.most_common(1)[0]
    if count >= 2:
        return iso, "verified"
    if site_conf == "verified" and iso == site_iso:
        return iso, "verified"
    return iso, "single_source"


_LI_COMPANY = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[A-Za-z0-9\-_%]+", re.I)


def _company_slug_matches(name: str, slug: str) -> bool:
    """Whether a LinkedIn slug plausibly IS this company.

    A prefix overlap is nowhere near enough: "colourandpitch" and "colourandstripe"
    share six characters, and "kscope" is a prefix of "kscopeai" (an unrelated AI
    company). Accepting those injects the wrong company into the pipeline, which
    SS52/SS53 forbid outright. We therefore require high full-string similarity.
    """
    flat_slug = slug.replace("-", "").replace("_", "").replace(".", "").lower()
    target = normalize_company_name(name).replace(" ", "")
    if not flat_slug or not target:
        return False
    if flat_slug == target:
        return True

    # Allow only a legal-suffix difference ("...ltd", "...limited", "...inc").
    for suffix in ("ltd", "limited", "inc", "llc", "group", "music", "records",
                   "recordings", "uk", "usa"):
        if flat_slug == target + suffix or target == flat_slug + suffix:
            return True

    from rapidfuzz import fuzz
    return fuzz.ratio(flat_slug, target) >= 92


_MUSIC_CONTEXT = re.compile(
    r"\b(record|records|recordings|label|music|artist|roster|band|album|"
    r"release|publishing|indie|independent)\b", re.I)


def _find_company_linkedin(ctx: Context, name: str) -> str:
    """Locate a company's LinkedIn page via search. '' when not confidently found.

    Two independent conditions must both hold: the slug must closely match the
    company name, AND the result text must read like a music company. Either alone
    produces false matches (SS53: never submit an unverified URL).
    """
    for query in (f'"{name}" record label linkedin company',
                  f'{name} site:linkedin.com/company'):
        for res in websearch.search(ctx.http, query, limit=8):
            blob = f"{res.title} {res.snippet}"
            for raw in _LI_COMPANY.findall(f"{res.url} {blob}"):
                slug = raw.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
                if not _company_slug_matches(name, slug):
                    continue
                if not _MUSIC_CONTEXT.search(blob):
                    continue
                return f"https://www.linkedin.com/company/{slug}"
    return ""


def verify_one(ctx: Context, row) -> dict:
    """Verify a single resolved company. Returns the facts dict used by the gates."""
    cid = row["company_id"]
    name = row["canonical_name"]
    site = website.fetch_site(ctx.http, row["website"]) if row["website"] else website.SiteData()

    # --- Wikidata + MusicBrainz corroboration --------------------------------
    wd = wikidata.lookup_company(ctx.http, name) or {}
    mb_iso = ""
    stored_mbid = row["mbid"] if "mbid" in row.keys() else ""
    mbid = stored_mbid or ""
    mb_candidates = musicbrainz.search_label(ctx.http, name, limit=1)
    if mb_candidates and mb_candidates[0]["score"] >= 90:
        mb_iso = (mb_candidates[0].get("country") or "").upper()
        # Derive the MBID here rather than depending on S2 having stored it, so the
        # SS6 roster fallback works even for companies resolved by an earlier version.
        mbid = mbid or mb_candidates[0]["mbid"]

    site_iso, site_conf = website.guess_country(site, row["domain"] or "")
    country, country_conf = _resolve_country(site_iso, site_conf,
                                             (wd.get("country") or "").upper(), mb_iso)

    # --- Roster (SS6) --------------------------------------------------------
    roster: RosterEstimate = site.roster
    dg_activity: dict = {}
    discogs_id = row["discogs_id"] if "discogs_id" in row.keys() else None

    # Discogs is consulted before MusicBrainz because its artist set is filtered to
    # RECENT releases, which is what SS6 means by an active roster; MusicBrainz
    # counts the whole catalogue and so overstates dormant labels (SS20).
    if discogs_id:
        dg_activity = discogs.label_activity(ctx.http, discogs_id) or {}
        if not roster.resolved and dg_activity.get("artist_count", 0) >= 3:
            n = dg_activity["artist_count"]
            lo, hi = max(3, n - 2), n + 2
            roster = RosterEstimate(
                low=lo, high=hi, display=f"Approximately {lo}-{hi} artists",
                evidence_url=dg_activity["evidence_url"], source="discogs")

    if not roster.resolved:
        # MusicBrainz artist count is corroboration only; SS20 warns that an old
        # catalogue does not evidence a current roster, so it yields a RANGE.
        mb_artists = musicbrainz.label_artists(ctx.http, mbid) if mbid else []
        if len(mb_artists) >= 3:
            lo, hi = max(3, len(mb_artists) - 3), len(mb_artists) + 3
            roster = RosterEstimate(low=lo, high=hi,
                                    display=f"Approximately {lo}-{hi} artists",
                                    evidence_url=f"https://musicbrainz.org/label/{mbid}",
                                    source="musicbrainz")

    # --- Company type (SS4) --------------------------------------------------
    tres = classify(name, site.description, site.text_blob,
                    has_roster=roster.resolved)

    # --- Socials (SS37) ------------------------------------------------------
    ig = site.socials.get("instagram", "")
    li = site.socials.get("linkedin", "")
    ig_url = instagram_url(ig) if ig else ""
    li_url = li if linkedin_slug(li)[0] == "company" else ""

    # The LinkedIn company URL is the key that unlocks people discovery (S5), so it
    # is worth resolving explicitly when the website footer did not carry one.
    # SS33 is clear that a missing company page is NOT grounds for rejection.
    if not li_url:
        li_url = _find_company_linkedin(ctx, name)

    # --- Cross-source activity confirmation (SS41 B3) ------------------------
    artist_rows = ctx.db.q("SELECT name FROM artists WHERE company_id = ? LIMIT 8", (cid,))
    known_artists = [r["name"] for r in artist_rows]
    confirm = {}
    if known_artists:
        confirm = itunes.confirm_label(ctx.http, name, known_artists,
                                       country=(country or "US").lower())

    # --- Persist -------------------------------------------------------------
    # Every fact is COALESCEd onto the stored value. A re-verification whose site
    # fetch times out returns empty facts, and a plain assignment would then wipe a
    # good roster or country -- silently dropping the company at the next gate and
    # taking its already-exported people with it. Absence of evidence in one fetch
    # is not evidence of absence (SS6/SS7), so a blank never overwrites a value.
    ctx.db.x("""UPDATE companies SET
                country=COALESCE(?, country),
                country_confidence=COALESCE(?, country_confidence),
                company_type=COALESCE(?, company_type),
                type_confidence=COALESCE(?, type_confidence),
                roster_estimate=COALESCE(?, roster_estimate),
                roster_low=COALESCE(?, roster_low),
                roster_high=COALESCE(?, roster_high),
                roster_display=COALESCE(?, roster_display),
                roster_evidence_url=COALESCE(?, roster_evidence_url),
                instagram_url=COALESCE(?, instagram_url),
                linkedin_url=COALESCE(?, linkedin_url),
                parent_org=COALESCE(?, parent_org), mbid=COALESCE(?, mbid),
                -- NOT COALESCEd like the other fields: a description that COALESCEs
                -- onto the old value means a company verified before this field
                -- stopped including raw page text (site.text_blob) stays permanently
                -- poisoned with it whenever a fresh fetch has no clean meta/Wikidata
                -- description to offer -- which is common for small labels. An empty
                -- description is safe (check_hard_exclusion treats blank input as
                -- CLEAR); a stale noisy one silently keeps hard-excluding a genuine
                -- label for a Spotify link or "recording studio" credit in its footer.
                description=?,
                status='VERIFIED', last_researched=?
                WHERE company_id=?""",
             (country or None, (country_conf if country else None),
              tres.company_type or None,
              (tres.confidence if tres.company_type else None),
              roster.midpoint, roster.low, roster.high, roster.display,
              roster.evidence_url or None, ig_url or None, li_url or None,
              wd.get("parent") or None, mbid or None,
              # SS8 exclusions are keyword checks against what the company says
              # about ITSELF. Deliberately NOT site.text_blob: a raw page dump
              # includes nav links, footer credits and embedded social/streaming
              # buttons, and a genuine label linking to Spotify or crediting its
              # "web design studio" in the footer was getting hard-excluded as a
              # "streaming service" or "production studio" for exactly that text.
              # site.description (the page's own <meta description>) and Wikidata's
              # formal description are both curated, self-description text; that is
              # the real distinction the gate needs, not more raw text.
              (" ".join(filter(None, [site.description, wd.get("description", "")]))
               or None),
              utcnow(), cid))

    for kind, url, source, strength in [
        ("roster", roster.evidence_url, roster.source, "strongest"),
        ("type", site.base_url, "website", "strongest"),
        ("country", wd.get("evidence_url", ""), "wikidata", "strong"),
        ("profile", ig_url, "website", "strongest"),
        ("activity", confirm.get("evidence_url", ""), "itunes", "strong"),
    ]:
        if url:
            ctx.db.x("""INSERT OR IGNORE INTO company_evidence
                        (company_id, kind, url, source, snippet, strength, retrieved_at)
                        VALUES (?,?,?,?,?,?,?)""",
                     (cid, kind, url, source, f"{kind} via {source}", strength, utcnow()))

    # SS27 categories mined from the company's own pages. SS41 B2 scores the number
    # of DISTINCT categories, so these are what lift a record above the release-only
    # ceiling of 7/10.
    for category, ev_url in website.detect_activity_signals(site).items():
        ctx.db.x("""INSERT OR IGNORE INTO activity_signals
                    (company_id, raw_label, signal_type, description, evidence_url,
                     observed_at, recorded_at, source, external_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (cid, name, category,
                  f"{category.replace('_', ' ')} evidenced on the company's own site",
                  ev_url, date.today().isoformat(), utcnow(), "website",
                  f"sig:{cid}:{category}"))
        ctx.db.x("""INSERT OR IGNORE INTO company_evidence
                    (company_id, kind, url, source, snippet, strength, retrieved_at)
                    VALUES (?,?,?,?,?,?,?)""",
                 (cid, "activity", ev_url, "website",
                  f"{category.replace('_', ' ')} (SS27)", "strongest", utcnow()))

    if dg_activity.get("recent_release_count"):
        ctx.db.x("""INSERT OR IGNORE INTO activity_signals
                    (company_id, raw_label, signal_type, description, evidence_url,
                     observed_at, recorded_at, source, external_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (cid, name, "release_activity",
                  f"Discogs: {dg_activity['recent_release_count']} releases in the last "
                  f"2 years across {dg_activity['artist_count']} artists; latest "
                  f"{dg_activity['latest_year']}",
                  dg_activity["evidence_url"], f"{dg_activity['latest_year']}-01-01",
                  utcnow(), "discogs", f"dg:{cid}"))
        ctx.db.x("""INSERT OR IGNORE INTO company_evidence
                    (company_id, kind, url, source, snippet, strength, retrieved_at)
                    VALUES (?,?,?,?,?,?,?)""",
                 (cid, "activity", dg_activity["evidence_url"], "discogs",
                  "recent release activity", "strong", utcnow()))
        for artist in dg_activity.get("artists", [])[:40]:
            ctx.db.x("""INSERT OR IGNORE INTO artists
                        (company_id, name, name_normalized, relationship, evidence_url,
                         recorded_at) VALUES (?,?,?,?,?,?)""",
                     (cid, artist, artist.casefold(), "label",
                      dg_activity["evidence_url"], utcnow()))

    if confirm.get("confirmed"):
        ctx.db.x("""INSERT OR IGNORE INTO activity_signals
                    (company_id, raw_label, signal_type, description, evidence_url,
                     observed_at, recorded_at, source, external_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (cid, name, "release_activity",
                  f"iTunes confirms {confirm['release_count']} releases "
                  f"({confirm['recent_count']} in the last year); latest "
                  f"{confirm['latest_release']}",
                  confirm.get("evidence_url", ""), confirm.get("latest_release", ""),
                  utcnow(), "itunes", f"confirm:{cid}"))

    for person in site.team:
        ctx.db.x("""INSERT OR IGNORE INTO people
                    (company_id, full_name, name_normalized, title, status, first_seen)
                    VALUES (?,?,?,?,?,?)""",
                 (cid, person.full_name, person.full_name.casefold(), person.title,
                  "CANDIDATE", utcnow()))

    return _facts(ctx, cid, name, site, tres, roster, country, country_conf, wd)


def _facts(ctx: Context, cid: int, name: str, site, tres, roster, country,
           country_conf, wd) -> dict:
    cutoff = (date.today() - timedelta(days=RECENT_DAYS)).isoformat()
    counts = ctx.db.one(
        """SELECT
             (SELECT COUNT(*) FROM artists WHERE company_id=:c) AS artists,
             (SELECT COUNT(*) FROM activity_signals WHERE company_id=:c) AS sigs,
             (SELECT COUNT(*) FROM activity_signals WHERE company_id=:c
               AND observed_at >= :cut) AS recent,
             (SELECT COUNT(*) FROM company_evidence WHERE company_id=:c) AS ev,
             (SELECT COUNT(*) FROM company_evidence WHERE company_id=:c
               AND strength IN ('strongest','strong')) AS strong""",
        {"c": cid, "cut": cutoff})
    return {
        "company_id": cid, "name": name,
        "description": site.description, "website_text": site.text_blob[:6000],
        "country": country, "country_confidence": country_conf,
        "company_type": tres.company_type, "type_confidence": tres.confidence,
        "parent_org": wd.get("parent", ""),
        "roster_estimate": roster.midpoint, "roster_resolved": roster.resolved,
        "artist_count": counts["artists"], "activity_signal_count": counts["sigs"],
        "recent_activity_count": counts["recent"], "evidence_urls": counts["ev"],
        "strong_evidence_count": counts["strong"],
    }


def run(ctx: Context, *, limit: int = 0) -> dict:
    rows = ctx.db.q("""SELECT company_id, canonical_name, website, domain, mbid,
                              discogs_id
                       FROM companies WHERE status='RESOLVED' ORDER BY company_id""")
    if limit:
        rows = rows[:limit]
    verified = failed = 0
    for row in rows:
        try:
            verify_one(ctx, row)
            verified += 1
        except Exception as exc:            # a broken site must not kill the run
            ctx.bump("verify_errors")
            ctx.db.x("UPDATE companies SET status='REVIEW', last_researched=? "
                     "WHERE company_id=?", (utcnow(), row["company_id"]))
            ctx.db.x("""INSERT INTO company_rejections
                        (company_id, gate, reason_code, reason_text, rejected_at, recheck_after)
                        VALUES (?,?,?,?,?,?)""",
                     (row["company_id"], "S3", "verify_error", str(exc)[:300],
                      utcnow(), utcnow()))
            failed += 1
    stats = {"verified": verified, "errors": failed}
    ctx.stats.setdefault("s3", {}).update(stats)
    return stats
