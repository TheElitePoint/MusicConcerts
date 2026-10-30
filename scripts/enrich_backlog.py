"""Fill the missing FACTS that hold companies in REVIEW/UNRESOLVED, using free sources.

Measured on a 150-company sample of the backlog, the blocking gates are:

    53  G1  country could not be determined      (SS7 forbids inferring it)
    51  G2  company category not established
    29  G6  roster could not be verified          (SS6: never guess)
    23  G5  only one artist evidenced
    16  G7  signals present but none recent

None of those is a gate being too strict -- they are facts nobody ever collected.
644 of these companies already carry an mbid or a discogs_id, so the facts are
reachable by ID lookup alone: no web search, no paid API, nothing to spend.

This script only ADDS evidence and then re-runs the unmodified gates. It never
relaxes a threshold, and a company whose facts stay unknown stays in REVIEW --
which is SS29 working, not a bug to code around.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mp.normalize import normalize_company_name, registrable_domain   # noqa: E402
from mp.pipeline import Context                           # noqa: E402
from mp.sources import discogs, musicbrainz, website      # noqa: E402
from mp.sop.gates import evaluate                         # noqa: E402
from mp.stages.s4_gates import build_facts                # noqa: E402
from mp.db import utcnow                                  # noqa: E402

# Hosts that are never the company's own site. These were resolved by an earlier
# version and poison every downstream fact ("Fontana" -> the City of Fontana, CA).
JUNK_HOSTS = (
    "wikipedia.org", "discogs.com", "facebook.com", "albumoftheyear.org",
    "linkedin.com", "youtube.com", "spotify.com", "apple.com", "instagram.com",
    "last.fm", "allmusic.com", "bandcamp.com", "twitter.com", "x.com",
    "soundcloud.com", "gov.uk", "service.gov.uk",
)


def is_junk_domain(domain: str) -> bool:
    """Whether a domain belongs to a directory/social host rather than the company.

    Matching is anchored on a label boundary. A bare substring test reads
    "square-enix.com" and "wolfspellrex.com" as the social host "x.com" and purges
    two real company sites, which is how this check first went wrong.
    """
    host = (domain or "").lower().strip().rstrip(".")
    if not host:
        return False
    if host.endswith(".gov") or ".gov." in host:
        return True
    return any(host == j or host.endswith("." + j) for j in JUNK_HOSTS)


def _evidence(ctx, cid, kind, url, source, snippet, strength):
    if not url:
        return
    ctx.db.x("""INSERT OR IGNORE INTO company_evidence
                (company_id, kind, url, source, snippet, strength, retrieved_at)
                VALUES (?,?,?,?,?,?,?)""",
             (cid, kind, url, source, snippet, strength, utcnow()))


def purge_junk(ctx) -> int:
    """Demote companies resolved to a domain that cannot be theirs (SS52/SS53)."""
    rows = ctx.db.q("""SELECT company_id, canonical_name, domain FROM companies
                       WHERE status IN ('REVIEW','UNRESOLVED','VERIFIED')
                         AND domain IS NOT NULL""")
    n = 0
    for row in rows:
        if is_junk_domain(row["domain"]):
            ctx.db.x("""UPDATE companies SET domain=NULL, website=NULL,
                        status='UNRESOLVED' WHERE company_id=?""", (row["company_id"],))
            print(f"  purged {row['canonical_name'][:32]:34s} {row['domain']}", flush=True)
            n += 1
    return n


def enrich_one(ctx, row) -> list[str]:
    """Add whatever free facts are missing. Returns the list of fields filled."""
    cid, filled = row["company_id"], []
    mbid, dgid = row["mbid"], row["discogs_id"]

    # --- website, for companies that never resolved one (no search needed) ----
    if not row["website"] and dgid:
        try:
            site_url = discogs.official_site(discogs.label_detail(ctx.http, dgid))
            if site_url and not is_junk_domain(registrable_domain(site_url)):
                ctx.db.x("UPDATE companies SET website=?, domain=? WHERE company_id=?",
                         (site_url, registrable_domain(site_url), cid))
                _evidence(ctx, cid, "profile", site_url, "discogs",
                          "official site listed on the Discogs label page", "strong")
                filled.append("website")
        except Exception:
            pass

    # --- G1 country ----------------------------------------------------------
    if not row["country"] and mbid:
        try:
            iso, ev = musicbrainz.label_country(ctx.http, mbid)
            if iso:
                ctx.db.x("""UPDATE companies SET country=?, country_confidence=?
                            WHERE company_id=?""", (iso, "single_source", cid))
                _evidence(ctx, cid, "country", ev, "musicbrainz",
                          f"MusicBrainz area resolves to {iso}", "strong")
                filled.append(f"country={iso}")
        except Exception:
            pass

    # --- G2 company type -----------------------------------------------------
    if not row["company_type"] and mbid:
        try:
            ctype, ev = musicbrainz.label_type(ctx.http, mbid)
            if ctype:
                ctx.db.x("""UPDATE companies SET company_type=?, type_confidence=?
                            WHERE company_id=?""", (ctype, "single_source", cid))
                _evidence(ctx, cid, "type", ev, "musicbrainz",
                          f"MusicBrainz label type maps to {ctype}", "strong")
                filled.append(f"type={ctype}")
        except Exception:
            pass

    # --- G5/G6 artists: the UNION of both catalogues -------------------------
    # Measured: Discogs and MusicBrainz disagree constantly (37 vs 54, 0 vs 21,
    # 2 vs 4). Either alone leaves companies short of the two-artist threshold
    # that neither source actually contradicts.
    known = {r["name_normalized"] for r in
             ctx.db.q("SELECT name_normalized FROM artists WHERE company_id=?", (cid,))}
    found: dict[str, tuple[str, str]] = {}
    if mbid:
        try:
            for name in musicbrainz.label_artists(ctx.http, mbid):
                found.setdefault(normalize_company_name(name),
                                 (name, f"https://musicbrainz.org/label/{mbid}"))
        except Exception:
            pass
    if dgid:
        try:
            act = discogs.label_activity(ctx.http, dgid)
            for name in (act.get("artists") or []):
                found.setdefault(normalize_company_name(name),
                                 (name, f"https://www.discogs.com/label/{dgid}"))
        except Exception:
            pass

    added = 0
    for norm, (name, ev) in found.items():
        if not norm or norm in known:
            continue
        ctx.db.x("""INSERT OR IGNORE INTO artists
                    (company_id, name, name_normalized, relationship, evidence_url,
                     recorded_at) VALUES (?,?,?,?,?,?)""",
                 (cid, name, norm, "roster", ev, utcnow()))
        added += 1
    if added:
        filled.append(f"artists+{added}")
        ev_url = next(iter(found.values()))[1]
        _evidence(ctx, cid, "roster", ev_url, "catalogue",
                  f"{len(found)} distinct artists across MusicBrainz/Discogs", "strong")

    # SS6: a roster number must be evidenced, never invented. The union count is
    # a real count of named artists, so it is a defensible estimate where none existed.
    if row["roster_estimate"] is None and len(found) >= 2:
        ctx.db.x("""UPDATE companies SET roster_estimate=?, roster_display=?,
                    roster_evidence_url=? WHERE company_id=?""",
                 (len(found), f"approximately {len(found)}",
                  next(iter(found.values()))[1], cid))
        filled.append(f"roster={len(found)}")

    # --- G1 second signal / G2 fallback: the company's own site --------------
    # Re-fetching now matters because the contact/Impressum page was never fetched
    # before, which is why guess_country returned nothing on sites with 4-5 pages.
    if row["website"] and (not row["country"] or row["country_confidence"] == "unknown"):
        try:
            site = website.fetch_site(ctx.http, row["website"])
            iso, conf = website.guess_country(site, row["domain"] or "")
            if iso:
                cur = ctx.db.one("SELECT country FROM companies WHERE company_id=?", (cid,))
                # Two independent sources agreeing is what SS7 calls verified.
                confidence = "verified" if (cur["country"] == iso) else conf
                ctx.db.x("""UPDATE companies SET country=?, country_confidence=?
                            WHERE company_id=?""", (iso, confidence, cid))
                _evidence(ctx, cid, "country", site.base_url, "website",
                          f"address/registration evidence on site resolves to {iso}",
                          "strongest")
                filled.append(f"site_country={iso}/{confidence}")
        except Exception:
            pass

    return filled


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--status", default="REVIEW,UNRESOLVED")
    ap.add_argument("--company-ids", default="", help="comma-separated, overrides --status")
    ap.add_argument("--skip-purge", action="store_true")
    args = ap.parse_args()

    ctx = Context.create(db_path="data/master.sqlite")
    try:
        if not args.skip_purge:
            print("purging non-company domains", flush=True)
            print(f"  {purge_junk(ctx)} purged\n", flush=True)

        if args.company_ids:
            ids = tuple(int(x) for x in args.company_ids.split(",") if x.strip())
            rows = ctx.db.q(f"""SELECT * FROM companies
                                WHERE company_id IN ({','.join('?' * len(ids))})""", ids)
        else:
            sts = tuple(s.strip() for s in args.status.split(","))
            rows = ctx.db.q(f"""SELECT * FROM companies
                                WHERE status IN ({','.join('?' * len(sts))})
                                ORDER BY company_id""", sts)
        if args.limit:
            rows = rows[:args.limit]

        print(f"enriching {len(rows)} companies from free sources", flush=True)
        stats = {"enriched": 0, "qualified": 0, "review": 0, "rejected": 0, "unchanged": 0}
        for i, row in enumerate(rows, 1):
            cid = row["company_id"]
            try:
                filled = enrich_one(ctx, row)
            except Exception as exc:
                print(f"  {i}/{len(rows)} skip {row['canonical_name'][:28]}: "
                      f"{type(exc).__name__}", flush=True)
                continue
            if not filled:
                stats["unchanged"] += 1
                continue
            stats["enriched"] += 1

            facts = build_facts(ctx, cid)
            if not facts:
                continue
            report = evaluate(facts)
            verdict = report.verdict
            ctx.db.x("""UPDATE companies SET gate_result=?, last_researched=?
                        WHERE company_id=?""",
                     (ctx.db.js(report.as_dict()), utcnow(), cid))
            new_status = {"PASS": "QUALIFIED", "REVIEW": "REVIEW",
                          "REJECT": "REJECTED"}[verdict]
            ctx.db.x("UPDATE companies SET status=? WHERE company_id=?", (new_status, cid))
            stats[{"QUALIFIED": "qualified", "REVIEW": "review",
                   "REJECTED": "rejected"}[new_status]] += 1
            if new_status == "QUALIFIED":
                print(f"  {i}/{len(rows)} QUALIFIED {row['canonical_name'][:30]:32s} "
                      f"{', '.join(filled)}", flush=True)
            elif i % 50 == 0:
                print(f"  {i}/{len(rows)} ...", flush=True)

        print(f"\n{stats}", flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
