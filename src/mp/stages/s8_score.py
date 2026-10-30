"""S8 -- Channel-specific scoring (SS39-SS46) and SS54 rationale generation.

Builds the structured facts dict for each (person, channel) pair and hands it to
sop/scoring.py. Nothing here interprets prose; every input is a counted, dated or
enumerated fact already stored in the database.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from ..db import utcnow
from ..pipeline import Context
from ..sop import narrative
from ..sop.exclusions import check_excluded_brand, check_hard_exclusion, is_approved_country
from ..sop.scoring import score

# Evidence-tier weights used for SS43 D1/D2.
STRENGTH_RANK = {"strongest": 3, "strong": 2, "weak": 1}


def _signal_ages(ctx: Context, cid: int) -> dict[str, int]:
    """Bucket activity signals by age in days, for SS41 B1."""
    buckets = {"30": 0, "90": 0, "180": 0, "365": 0}
    today = date.today()
    for row in ctx.db.q("""SELECT observed_at FROM activity_signals
                           WHERE company_id=? AND observed_at IS NOT NULL
                             AND observed_at != ''""", (cid,)):
        raw = (row["observed_at"] or "")[:10]
        try:
            age = (today - datetime.strptime(raw, "%Y-%m-%d").date()).days
        except ValueError:
            continue
        for edge in (30, 90, 180, 365):
            if age <= edge:
                buckets[str(edge)] += 1
    return buckets


def build_facts(ctx: Context, person_id: int) -> dict:
    """Assemble every fact the SS40-SS46 scorer needs."""
    row = ctx.db.one("""SELECT p.*, c.company_id, c.canonical_name, c.website, c.domain,
                               c.country, c.company_type, c.type_confidence,
                               c.roster_estimate, c.roster_display, c.roster_evidence_url,
                               c.instagram_url AS company_ig, c.linkedin_url AS company_li,
                               c.parent_org
                        FROM people p JOIN companies c USING(company_id)
                        WHERE p.person_id=?""", (person_id,))
    if row is None:
        return {}

    cid = row["company_id"]
    # Same 12-month window as the company activity gate (Company SOP 01 v4.0).
    cutoff = (date.today() - timedelta(days=365)).isoformat()

    counts = ctx.db.one(
        """SELECT
             (SELECT COUNT(*) FROM artists WHERE company_id=:c) AS artists,
             (SELECT COUNT(DISTINCT signal_type) FROM activity_signals
               WHERE company_id=:c) AS categories,
             (SELECT COUNT(DISTINCT source) FROM activity_signals
               WHERE company_id=:c) AS sources,
             (SELECT COUNT(*) FROM activity_signals
               WHERE company_id=:c AND observed_at >= :cut) AS recent,
             (SELECT COUNT(*) FROM company_evidence WHERE company_id=:c) AS ev""",
        {"c": cid, "cut": cutoff})

    # Client rule 2026-10-25: 6+ distinct promoted concerts in the PAST 12 months or
    # in the NEXT 12 months -- each window counted on its own, never summed.
    live = ctx.db.one("""SELECT
          COUNT(DISTINCT CASE WHEN observed_at >= date('now','-365 days')
                               AND observed_at < date('now') THEN external_id END) past,
          COUNT(DISTINCT CASE WHEN observed_at >= date('now')
                               AND observed_at <= date('now','+365 days') THEN external_id END) next
        FROM activity_signals WHERE company_id=? AND signal_type='live_event'""", (cid,))
    live = {"past": live["past"], "next": live["next"], "n": max(live["past"], live["next"])}

    def best_strength(kinds: tuple[str, ...]) -> int:
        placeholders = ",".join("?" * len(kinds))
        rows = ctx.db.q(f"""SELECT strength FROM company_evidence
                            WHERE company_id=? AND kind IN ({placeholders})""",
                        (cid, *kinds))
        return max((STRENGTH_RANK.get(r["strength"], 0) for r in rows), default=0)

    profiles = {p["channel"]: p for p in
                ctx.db.q("SELECT * FROM profiles WHERE person_id=?", (person_id,))}

    signal_categories = [r["signal_type"] for r in ctx.db.q(
        "SELECT DISTINCT signal_type FROM activity_signals WHERE company_id=?", (cid,))]

    # SS47/SS48 make "Activity Evidence URL" a MANDATORY column, so it must resolve to
    # a real URL. Prefer the strongest-tier activity evidence; fall back to the most
    # recent dated activity signal.
    activity_ev = ctx.db.one(
        """SELECT url FROM company_evidence WHERE company_id=? AND kind='activity'
           ORDER BY CASE strength WHEN 'strongest' THEN 0 WHEN 'strong' THEN 1
                    ELSE 2 END, retrieved_at DESC LIMIT 1""", (cid,))
    if not activity_ev:
        activity_ev = ctx.db.one(
            """SELECT evidence_url AS url FROM activity_signals
               WHERE company_id=? AND evidence_url IS NOT NULL AND evidence_url != ''
               ORDER BY observed_at DESC LIMIT 1""", (cid,))

    hard = check_hard_exclusion(row["canonical_name"])
    brand = check_excluded_brand(row["canonical_name"], row["parent_org"] or "")

    facts = {
        # -- company identity (also used for the sheet columns) --
        "company_id": cid, "company_name": row["canonical_name"],
        "company_website": row["website"] or "", "company_ig": row["company_ig"] or "",
        "company_li": row["company_li"] or "", "country": row["country"] or "",
        "company_type": row["company_type"] or "",
        "type_confidence": row["type_confidence"] or "none",
        "roster_estimate": row["roster_estimate"],
        "roster_display": row["roster_display"] or "",
        "roster_evidence_url": row["roster_evidence_url"] or "",
        # -- Projects LIVE lane (Company SOP 01 v4.0 SS4): promoter of record on
        #    distinct music events inside a 12-month window --
        "lane": "LIVE" if (row["company_type"] == "concert_festival"
                           and live["n"] >= 6) else "PLATFORM",
        "live_event_count": live["n"],
        "live_events_past": live["past"], "live_events_next": live["next"],
        # -- SS40 A --
        "artists_evidenced": counts["artists"],
        # -- SS41 B --
        "signals_by_age": _signal_ages(ctx, cid),
        "distinct_signal_categories": counts["categories"],
        "signal_categories": signal_categories,
        "credible_activity_sources": counts["sources"],
        "weak_activity_sources": counts["sources"],
        "recent_activity_count": counts["recent"],
        "activity_evidence_url": activity_ev["url"] if activity_ev else "",
        # -- SS42 C --
        "person_id": person_id, "person_name": row["full_name"],
        "person_title": row["title"] or "", "title_family": row["title_family"] or "",
        "role_relevance": row["role_relevance"] or 0,
        "seniority": row["seniority"] or "junior",
        "employment_sources": row["employment_sources"] or 0,
        "employment_claimed": bool(row["employment_sources"]),
        # -- SS43 D --
        "company_evidence_strength": best_strength(("roster", "type", "identity")),
        "activity_evidence_strength": best_strength(("activity",)),
        "evidence_urls": counts["ev"],
        # -- SS46 hard gates --
        "approved_country": is_approved_country(row["country"] or ""),
        "hard_excluded": hard.excluded,
        "brand_excluded": brand.excluded,
        "current_company_verified": bool(row["current_verified"]),
        "current_title_verified": bool(row["current_verified"]),
        "unresolved_duplicate": False,
    }

    for channel in ("linkedin", "instagram"):
        prof = profiles.get(channel)
        facts[f"{channel}_url"] = prof["url"] if prof else ""
        facts[f"{channel}_verified"] = bool(
            prof and prof["verification_status"] == "VERIFIED")
        facts[f"{channel}_usability"] = (prof["usability"] if prof and prof["usability"]
                                         else "none")
        if channel == "instagram" and prof:
            facts["instagram_status"] = prof["verification_status"]
    facts.setdefault("instagram_status", "NOT FOUND")
    return facts


def run(ctx: Context, *, limit: int = 0) -> dict:
    rows = ctx.db.q("""SELECT p.person_id FROM people p JOIN companies c USING(company_id)
                       WHERE c.status='QUALIFIED' AND p.status IN ('VERIFIED','REVIEW')
                       ORDER BY p.role_relevance DESC""")
    if limit:
        rows = rows[:limit]

    from .. import config
    sop_version = config.scoring()["sop_version"]
    tally = {"scored": 0, "linkedin_pass": 0, "instagram_pass": 0,
             "research_further": 0, "not_final": 0}

    for row in rows:
        pid = row["person_id"]
        facts = build_facts(ctx, pid)
        if not facts:
            continue

        why_company = narrative.generate("company", facts, http=ctx.http)
        why_person = narrative.generate("person", facts, http=ctx.http)

        for channel in ("linkedin", "instagram"):
            result = score(facts, channel)
            ctx.db.x("""INSERT OR REPLACE INTO scores
                        (person_id, channel, subscores, total, verdict, failed_gates,
                         why_company, why_person, sop_version, scored_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (pid, channel, ctx.db.js(result.subscores), result.total,
                      result.verdict, ctx.db.js(result.failed_gates),
                      why_company, why_person, sop_version, utcnow()))
            if result.verdict == "FINAL PASS":
                tally[f"{channel}_pass"] += 1
            elif result.verdict == "RESEARCH FURTHER":
                tally["research_further"] += 1
            else:
                tally["not_final"] += 1
        tally["scored"] += 1

    ctx.stats.setdefault("s8", {}).update(tally)
    return tally
