"""S1 -- Activity-first discovery (SS3, SS12-SS21, SS65 Stage 1).

"Do not begin by scraping thousands of generic music companies. The strongest
prospects usually begin with evidence of current market activity."

So every company entering this pipeline arrives attached to a real, dated, evidenced
release or campaign. We never enumerate a company directory and work forwards.

Cost discipline: the alias skip-index is consulted BEFORE any per-company network
call, and cheap name-only exclusion checks (SS8/SS9) run before any enrichment, so
excluded companies cost approximately nothing.
"""
from __future__ import annotations

from collections import defaultdict

from .. import config, used_contacts
from ..db import utcnow
from ..models import RawSignal
from ..normalize import loose_company_key, normalize_company_name
from ..pipeline import Context
from ..sop.exclusions import check_excluded_brand, check_hard_exclusion
from ..sources import itunes, musicbrainz, spotify

# Discovery-time signal grouping: how many distinct artists constitute the SS27
# "multi-artist activity" signal, which the SOP calls "especially strong".
MULTI_ARTIST_THRESHOLD = 2


def _collect(ctx: Context, country: str, lookback: int) -> list[RawSignal]:
    """Gather raw activity signals from every enabled free discovery source."""
    enabled = config.sources()["discovery"]["enabled"]
    signals: list[RawSignal] = []

    if "musicbrainz" in enabled:
        # Primary source: one query returns dozens of active labels in-country,
        # with the label name inline. Keyless and unlimited.
        # Paginate on RELEASE count, not signal count -- many releases carry no
        # label-info, so signal count would stop the crawl far too early.
        max_pages = config.sources()["discovery"].get("mb_pages", 5)
        for page in range(max_pages):
            batch, n_releases = musicbrainz.recent_releases(
                ctx.http, country, lookback_days=lookback,
                limit=100, offset=page * 100)
            signals.extend(batch)
            ctx.bump("mb_signals", len(batch))
            if n_releases < 100:
                break

    if "spotify" in enabled and spotify.available(ctx.http):
        # SS12: new releases -> album.label. Four calls cover a country's feed,
        # and it is the freshest label signal available anywhere.
        batch = spotify.new_releases(ctx.http, country, limit=50)
        signals.extend(batch)
        ctx.bump("spotify_signals", len(batch))

    if "itunes" in enabled:
        # Secondary discovery seed. iTunes cannot sort by date, so it is used with
        # genre terms and then date-filtered; its main value is cross-confirmation
        # in S3, not raw discovery.
        for term in ("indie", "electronic", "hip hop", "alternative", "pop",
                     "rock", "dance", "r&b", "metal", "folk", "jazz",
                     "country", "latin", "reggae", "punk"):
            batch = itunes.search_albums(ctx.http, term, country,
                                         limit=50, lookback_days=lookback)
            signals.extend(batch)
            ctx.bump("itunes_signals", len(batch))

    return signals


def _prefilter(name: str) -> tuple[bool, str, str]:
    """Cheap name-only rejection before ANY enrichment spend.

    Returns (should_reject, gate, reason). Full gating happens in S4 with real
    evidence; this only catches what is already obvious from the name.
    """
    brand = check_excluded_brand(name)
    if brand.excluded:
        return True, "G4", brand.reason
    hard = check_hard_exclusion(name)
    if hard.excluded:
        return True, "G3", hard.reason
    return False, "", ""


def run(ctx: Context, *, lookback_days: int | None = None) -> dict:
    """Discover candidate companies from current activity. Returns run stats."""
    src_cfg = config.sources()["discovery"]
    lookback = lookback_days or src_cfg["lookback_days"]
    countries = ctx.countries or ["GB"]

    grouped: dict[str, list[RawSignal]] = defaultdict(list)
    for country in countries:
        for sig in _collect(ctx, country, lookback):
            key = normalize_company_name(sig.raw_label)
            if key:
                grouped[key].append(sig)

    ctx.bump("labels_seen", len(grouped))
    created = skipped = rejected = attached = 0

    for key, sigs in grouped.items():
        raw_name = sigs[0].raw_label

        # Already-used company (client exclusion list): do not research it again.
        if used_contacts.company_blocked(raw_name):
            skipped += 1
            ctx.bump("skipped_used_company")
            continue

        # --- 1. SKIP INDEX: has prior research already settled this company? ---
        company_id = ctx.db.alias_lookup(key)
        if company_id is not None:
            should_skip, why = ctx.db.should_skip_company(company_id)
            if should_skip:
                skipped += 1
                ctx.bump("skipped_known")
                continue
            attached += 1
        else:
            # --- 2. CHEAP EXCLUSION before any enrichment spend ---
            reject, gate, reason = _prefilter(raw_name)
            if reject:
                company_id = _insert_company(ctx, raw_name, sigs, status="REJECTED")
                ctx.db.x("""INSERT INTO company_rejections
                            (company_id, gate, reason_code, reason_text, rejected_at, recheck_after)
                            VALUES (?,?,?,?,?,NULL)""",
                         (company_id, gate, gate + "_prefilter", reason, utcnow()))
                ctx.db.add_alias(key, company_id, raw_name, sigs[0].source)
                rejected += 1
                ctx.bump("rejected_prefilter")
                continue
            company_id = _insert_company(ctx, raw_name, sigs, status="NEW")
            created += 1

        ctx.db.add_alias(key, company_id, raw_name, sigs[0].source)
        _persist_signals(ctx, company_id, sigs)

        if ctx.limit and created >= ctx.limit:
            break

    stats = {"labels_seen": len(grouped), "created": created, "attached": attached,
             "skipped_known": skipped, "rejected_prefilter": rejected}
    ctx.stats.setdefault("s1", {}).update(stats)
    return stats


def _insert_company(ctx: Context, name: str, sigs: list[RawSignal], *, status: str) -> int:
    cur = ctx.db.x("""INSERT INTO companies
                      (canonical_name, name_normalized, name_loose, country,
                       country_confidence, status, first_seen, discovered_via)
                      VALUES (?,?,?,?,?,?,?,?)""",
                   (name, normalize_company_name(name), loose_company_key(name),
                    sigs[0].country_hint or None, "unknown", status, utcnow(),
                    sigs[0].source))
    return cur.lastrowid


def _persist_signals(ctx: Context, company_id: int, sigs: list[RawSignal]) -> None:
    """Store evidence, artists, and derive the SS27 multi-artist signal."""
    artists: set[str] = set()

    for sig in sigs:
        ctx.db.x("""INSERT OR IGNORE INTO activity_signals
                    (company_id, raw_label, signal_type, description, evidence_url,
                     observed_at, recorded_at, source, external_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (company_id, sig.raw_label, sig.signal_type, sig.description,
                  sig.evidence_url, sig.observed_at, utcnow(), sig.source,
                  sig.external_id))
        # Release pages are official release information -> "strongest" tier (SS38).
        ctx.db.x("""INSERT OR IGNORE INTO company_evidence
                    (company_id, kind, url, source, snippet, strength, retrieved_at)
                    VALUES (?,?,?,?,?,?,?)""",
                 (company_id, "activity", sig.evidence_url, sig.source,
                  sig.description[:300], "strong", utcnow()))
        for artist in sig.artists:
            if artist:
                artists.add(artist)

    for artist in artists:
        ctx.db.x("""INSERT OR IGNORE INTO artists
                    (company_id, name, name_normalized, relationship, evidence_url, recorded_at)
                    VALUES (?,?,?,?,?,?)""",
                 (company_id, artist, normalize_company_name(artist), "label",
                  sigs[0].evidence_url, utcnow()))

    # SS27: "One active artist is weaker evidence than six active artists."
    if len(artists) >= MULTI_ARTIST_THRESHOLD:
        ctx.db.x("""INSERT OR IGNORE INTO activity_signals
                    (company_id, raw_label, signal_type, description, evidence_url,
                     observed_at, recorded_at, source, external_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (company_id, sigs[0].raw_label, "multi_artist_activity",
                  f"{len(artists)} distinct artists with releases in the discovery window: "
                  + ", ".join(sorted(artists)[:6]),
                  sigs[0].evidence_url, sigs[0].observed_at, utcnow(),
                  "derived", f"multi:{company_id}"))
