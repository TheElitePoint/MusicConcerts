"""S7 -- Person verification (SS32 Steps 3-7, SS34, SS35, SS36).

Two independent verifications:

  EMPLOYMENT (SS32/SS42 C3) -- current company and title. SS42 states that a record
  with materially uncertain current employment cannot receive FINAL PASS, so this
  is a hard gate, not a scoring nudge.

  IDENTITY (SS34 LinkedIn / SS35 Instagram) -- that the profile belongs to THIS
  person at THIS company. SS35 is explicit that a lookalike username proves nothing,
  so username similarity is scored at zero and a threshold of independent signals
  is required.
"""
from __future__ import annotations

from .. import config
from ..db import utcnow
from ..normalize import instagram_handle, normalize_company_name, normalize_person_name
from ..pipeline import Context
from ..sop.roles import classify_title
from ..sources import apify


def instagram_identity_score(*, bio: str, full_name: str, person: str,
                             company: str, external_links: list[str],
                             company_ig: str = "",
                             roster_artists: list[str] | None = None) -> tuple[int, dict]:
    """Score SS35 identity signals. Returns (total, which_signals_fired)."""
    weights = config.sop()["instagram_identity_signals"]
    fired: dict[str, bool] = {}
    blob = f"{bio} {' '.join(external_links)}".lower()
    company_norm = normalize_company_name(company)

    fired["company_in_bio"] = bool(company_norm and company_norm in
                                   normalize_company_name(bio))
    fired["role_in_bio"] = any(k in blob for k in
                               ("founder", "ceo", "head of", "director", "a&r",
                                "manager", "marketing", "label"))
    fired["company_tagged"] = bool(company_ig and
                                   instagram_handle(company_ig) and
                                   instagram_handle(company_ig) in blob)
    fired["roster_artists_appear"] = any(
        a.lower() in blob for a in (roster_artists or [])[:10] if len(a) > 3)
    fired["external_links_confirm"] = any(
        company_norm and company_norm.replace(" ", "") in link.lower().replace("-", "")
        for link in external_links)
    fired["matches_linkedin_identity"] = bool(
        full_name and normalize_person_name(full_name) == normalize_person_name(person))
    # SS35: explicitly worth nothing on its own.
    fired["username_resembles_name"] = False

    total = sum(weights.get(name, 0) for name, hit in fired.items() if hit)
    return total, fired


# SS38 ranks "verified/current LinkedIn profile" and "official team page" in the
# STRONGEST evidence tier. A person's own current LinkedIn position, or their
# appearance on the employer's own team page, is direct first-party proof of
# current employment -- so either satisfies SS42 C3 on its own. Weaker sources
# (search snippets, articles) still require corroboration.
STRONGEST_EMPLOYMENT_SOURCES = ("apify_linkedin", "website_team", "website_about",
                                "website")


def verify_employment(ctx: Context, person_id: int) -> tuple[int, bool]:
    """Assess current employment (SS32 Steps 3-4, SS42 C3).

    Returns (source_count, verified). Verified when either two independent sources
    agree, or one STRONGEST-tier first-party source confirms it.
    """
    rows = ctx.db.q("""SELECT DISTINCT source, strength FROM person_evidence
                       WHERE person_id=? AND kind IN ('employment','title')""",
                    (person_id,))
    sources = {r["source"] for r in rows}
    has_strongest = any(
        s.startswith(STRONGEST_EMPLOYMENT_SOURCES) for s in sources
    ) or any(r["strength"] == "strongest" for r in rows)

    count = len(sources)
    if has_strongest:
        # Counts as two for SS42 C3 scoring: first-party and independently checkable.
        count = max(count, 2)
    return count, (count >= 2)


def run(ctx: Context, *, limit: int = 0) -> dict:
    # CANDIDATE/REVIEW always need a first pass. An already-VERIFIED person is
    # only re-checked after a cooldown -- catches drift (e.g. a profile going
    # private) without burning a full re-verify every ~20s mining-loop cycle.
    rows = ctx.db.q("""SELECT p.person_id, p.full_name, p.title, c.canonical_name,
                              c.instagram_url AS company_ig, c.company_id,
                              c.roster_estimate
                       FROM people p JOIN companies c USING(company_id)
                       WHERE c.status='QUALIFIED'
                         AND (p.status IN ('CANDIDATE','REVIEW')
                              OR (p.status='VERIFIED'
                                  AND (p.last_researched IS NULL
                                       OR p.last_researched < datetime('now','-1 hour'))))""")
    if limit:
        rows = rows[:limit]

    verified = li_ok = ig_ok = 0
    min_score = config.sop()["instagram_identity_signals"]["min_score_to_verify"]

    for row in rows:
        pid = row["person_id"]
        # Re-run SS30/SS31 at verification time.  This catches legacy records and
        # composite titles where a senior word belongs to the executive being
        # supported ("Executive Assistant to CEO"), not to the candidate.
        title_match = classify_title(
            row["title"] or "",
            small_company=(row["roster_estimate"] is not None
                           and row["roster_estimate"] <= 20),
        )
        if not title_match.qualifies:
            ctx.db.x("""UPDATE people SET status='REJECTED', current_verified=0,
                        reject_reason=?, last_researched=? WHERE person_id=?""",
                     (title_match.exclusion_reason or "No SS30 role family match",
                      utcnow(), pid))
            continue
        ctx.db.x("""UPDATE people SET title_family=?, seniority=?, role_relevance=?
                    WHERE person_id=? AND status != 'REJECTED'""",
                 (title_match.family, title_match.seniority,
                  title_match.relevance, pid))
        sources, current = verify_employment(ctx, pid)
        # Never resurrect a REJECTED person: this stage loads its rows up front and
        # runs for minutes, so a rejection made meanwhile (manual review, audit
        # script) must not be overwritten by a stale VERIFIED.
        ctx.db.x("""UPDATE people SET employment_sources=?, current_verified=?,
                    status=?, last_researched=? WHERE person_id=? AND status != 'REJECTED'""",
                 (sources, int(current), "VERIFIED" if current else "REVIEW",
                  utcnow(), pid))
        verified += int(current)

        artists = [r["name"] for r in ctx.db.q(
            "SELECT name FROM artists WHERE company_id=? LIMIT 10", (row["company_id"],))]

        for prof in ctx.db.q("SELECT * FROM profiles WHERE person_id=?", (pid,)):
            channel = prof["channel"]

            if channel == "linkedin":
                # SS34: the slug already corroborated the name in S6; current
                # employment is what remains, and it is shared with the person record.
                status = "VERIFIED" if current else "UNVERIFIED"
                usability = "full" if current else "partial"
                ctx.db.x("""UPDATE profiles SET verification_status=?, usability=?,
                            verified_at=? WHERE profile_id=?""",
                         (status, usability, utcnow(), prof["profile_id"]))
                li_ok += int(status == "VERIFIED")

            else:
                bio = full_name = ""
                links: list[str] = []
                private = False
                fetched = False
                paid = config.sources()["profiles"].get("paid_channels",
                                                         ["linkedin", "instagram"])
                if "instagram" in paid and apify.available(ctx.http):
                    res = apify.fetch_instagram_profile(
                        ctx.http, prof["url"],
                        conditions={"company_passed_all_gates": True,
                                    "person_has_verified_name": True,
                                    "person_title_in_role_family": True,
                                    "no_cached_profile": True,
                                    "free_sources_exhausted": True})
                    bio, full_name = res.bio, res.full_name
                    links, private = res.external_links, res.is_private
                    # A record can come back with every signal field empty (private
                    # account, partial scrape, actor stub). Scoring that yields 0 and
                    # flips a good verdict, so it counts as NOT fetched: SS35 needs
                    # actual bio/name/link content to judge identity at all.
                    fetched = bool(res.found and (res.bio or res.full_name
                                                  or res.external_links))

                # SS35 scoring requires the profile's bio/links. Without fresh data
                # the score would be computed from EMPTY inputs, which always yields
                # 0 and flips the verdict. A failed or skipped fetch is not evidence
                # about the identity either way, so the stored verdict is left
                # completely untouched -- in BOTH directions. Protecting only the
                # VERIFIED case still let a profile flap to UNVERIFIED once and then
                # stay there, which is what took Instagram Final back to 0.
                if not fetched:
                    ig_ok += int(prof["verification_status"] == "VERIFIED")
                    continue

                score, fired = instagram_identity_score(
                    bio=bio, full_name=full_name, person=row["full_name"],
                    company=row["canonical_name"], external_links=links,
                    company_ig=row["company_ig"] or "", roster_artists=artists)

                ok = score >= min_score and current
                ctx.db.x("""UPDATE profiles SET verification_status=?, identity_score=?,
                            usability=?, match_signals=?, verified_at=?
                            WHERE profile_id=?""",
                         ("VERIFIED" if ok else "UNVERIFIED", score,
                          "full" if ok and not private else
                          ("partial" if ok else "weak"),
                          ctx.db.js(fired), utcnow(), prof["profile_id"]))
                ig_ok += int(ok)

    stats = {"employment_verified": verified, "linkedin_verified": li_ok,
             "instagram_verified": ig_ok, "people": len(rows)}
    ctx.stats.setdefault("s7", {}).update(stats)
    return stats
