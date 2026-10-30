"""S4 -- Apply the SS29 company qualification gate.

This is the cost firewall. A company that does not clear all eight gates never
reaches person research, and therefore never costs an Apify call.

Outcomes are persisted permanently:
  PASS   -> status QUALIFIED, proceeds to S5
  REJECT -> status REJECTED + a rejection row with the failing gate and reason
  REVIEW -> status REVIEW, routed to the review queue with what is missing
"""
from __future__ import annotations

from datetime import date, timedelta

from ..db import utcnow
from ..pipeline import Context
from ..sop.gates import evaluate

# Company SOP 01 v4.0 counts activity over a 12-month window (SS4.2); the old
# 180-day window was internal and held ~370 otherwise-qualified companies in REVIEW.
RECENT_DAYS = 365

# How long before a REJECT is reconsidered. Category and brand exclusions are
# permanent facts about the company; missing evidence is not.
RECHECK_DAYS = {
    "G1": None,      # geography does not change
    "G2": 180,       # a company can pivot
    "G3": None,      # hard-excluded category
    "G4": None,      # excluded core brand
    "G5": 90,
    "G6": 90,
    "G7": 60,        # activity is time-sensitive by definition
    "G8": 60,
}


def build_facts(ctx: Context, cid: int) -> dict:
    """Assemble the structured facts the gates evaluate, straight from the DB."""
    row = ctx.db.one("""SELECT company_id, canonical_name, website, domain, country,
                               country_confidence, company_type, type_confidence,
                               roster_estimate, roster_low, roster_high, parent_org,
                               instagram_url, linkedin_url, description
                        FROM companies WHERE company_id=?""", (cid,))
    if row is None:
        return {}

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
        "company_id": cid,
        "name": row["canonical_name"],
        "description": row["description"] or "",
        "website_text": row["description"] or "",
        "country": row["country"] or "",
        "country_confidence": row["country_confidence"] or "unknown",
        "company_type": row["company_type"] or "",
        "type_confidence": row["type_confidence"] or "none",
        "parent_org": row["parent_org"] or "",
        "domain": row["domain"] or "",
        "instagram_url": row["instagram_url"] or "",
        "linkedin_url": row["linkedin_url"] or "",
        "roster_estimate": row["roster_estimate"],
        "roster_resolved": row["roster_estimate"] is not None,
        "artist_count": counts["artists"],
        "activity_signal_count": counts["sigs"],
        "recent_activity_count": counts["recent"],
        "evidence_urls": counts["ev"],
        "strong_evidence_count": counts["strong"],
    }


def run(ctx: Context, *, limit: int = 0) -> dict:
    rows = ctx.db.q("""SELECT company_id FROM companies
                       WHERE status='VERIFIED' ORDER BY company_id""")
    if limit:
        rows = rows[:limit]

    qualified = rejected = review = 0

    for row in rows:
        cid = row["company_id"]
        facts = build_facts(ctx, cid)
        if not facts:
            continue
        report = evaluate(facts)
        verdict = report.verdict

        ctx.db.x("UPDATE companies SET gate_result=?, last_researched=? WHERE company_id=?",
                 (ctx.db.js(report.as_dict()), utcnow(), cid))

        if verdict == "REJECT":
            gate = report.first_failure()
            days = RECHECK_DAYS.get(gate.id)
            recheck = None
            if days:
                recheck = (date.today() + timedelta(days=days)).isoformat()
            ctx.db.x("UPDATE companies SET status='REJECTED' WHERE company_id=?", (cid,))
            ctx.db.x("""INSERT INTO company_rejections
                        (company_id, gate, reason_code, reason_text, rejected_at, recheck_after)
                        VALUES (?,?,?,?,?,?)""",
                     (cid, gate.id, f"{gate.id}_fail", gate.reason, utcnow(), recheck))
            rejected += 1
        elif verdict == "REVIEW":
            ctx.db.x("UPDATE companies SET status='REVIEW' WHERE company_id=?", (cid,))
            review += 1
        else:
            ctx.db.x("UPDATE companies SET status='QUALIFIED' WHERE company_id=?", (cid,))
            qualified += 1

    stats = {"qualified": qualified, "rejected": rejected, "review": review}
    ctx.stats.setdefault("s4", {}).update(stats)
    return stats
