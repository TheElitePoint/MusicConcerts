"""Upgrade search-snippet employment evidence to first-party LinkedIn evidence.

SS38 ranks a scraped snippet as WEAK and a current LinkedIn profile as STRONGEST.
People discovered from search results therefore cannot clear the SS42 C3 employment
hard gate on that evidence alone. Fetching their actual profiles - many per Apify
run, $0.004 each - supplies the first-party current-position claim the SOP wants.
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mp.db import utcnow                                    # noqa: E402
from mp.normalize import normalize_company_name             # noqa: E402
from mp.pipeline import Context                             # noqa: E402
from mp.sources import apify                                # noqa: E402

CONDITIONS = {"company_passed_all_gates": True, "person_has_verified_name": True,
              "person_title_in_role_family": True, "no_cached_profile": True,
              "free_sources_exhausted": True}


def main(limit: int = 0) -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        sql = """SELECT p.person_id, p.full_name, c.canonical_name AS company,
                                  pr.url
                           FROM people p
                           JOIN companies c ON c.company_id = p.company_id
                           JOIN profiles pr ON pr.person_id = p.person_id
                                           AND pr.channel = 'linkedin'
                           WHERE c.status = 'QUALIFIED'
                             AND p.status != 'REJECTED'
                             AND p.current_verified = 0
                             AND pr.url IS NOT NULL
                             AND EXISTS (SELECT 1 FROM person_evidence e
                                          WHERE e.person_id=p.person_id
                                            AND e.source='apify_google_serp')
                           ORDER BY p.role_relevance DESC,
                                    CASE p.seniority WHEN 'executive' THEN 0 WHEN 'senior' THEN 1 ELSE 2 END,
                                    p.person_id"""
        if limit > 0:
            sql += " LIMIT ?"
            rows = ctx.db.q(sql, (limit,))
        else:
            rows = ctx.db.q(sql)
        print(f"{len(rows)} people need first-party employment evidence", flush=True)
        if not rows:
            return 0

        hits = apify.fetch_profiles_batch(ctx.http, [r["url"] for r in rows],
                                          conditions=CONDITIONS)
        print(f"fetched {len(hits)} profile records", flush=True)

        # Index the results by NAME as well as by URL. Our stored URLs are often
        # LinkedIn's obfuscated member URNs (/in/ACwAA...), while the scraper returns
        # the canonical vanity URL (/in/conor-mackie) -- so URL-only matching silently
        # matched nothing and no one was ever confirmed.
        from mp.normalize import normalize_person_name
        by_name = {}
        for hit in hits.values():
            if hit.full_name:
                by_name.setdefault(normalize_person_name(hit.full_name), hit)

        confirmed = mismatched = 0
        for row in rows:
            url = (row["url"] or "").lower()
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            hit = (hits.get(url) or hits.get(slug)
                   or by_name.get(normalize_person_name(row["full_name"])))
            if not hit or not hit.company:
                continue
            expected = normalize_company_name(row["company"])
            found = normalize_company_name(hit.company)
            if expected and found and not (expected in found or found in expected):
                # SS34: this is a same-name / wrong-company mismatch. Record it so the
                # person is never reconsidered and never reaches a final sheet.
                ctx.db.x("""UPDATE people SET status='REJECTED', reject_reason=?,
                            last_researched=? WHERE person_id=?""",
                         (f"SS34 employer mismatch: LinkedIn shows '{hit.company}', "
                          f"not '{row['company']}'", utcnow(), row["person_id"]))
                mismatched += 1
                continue
            ctx.db.x("""INSERT INTO person_evidence
                        (person_id, kind, url, source, snippet, strength, retrieved_at)
                        VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(person_id, kind, url) DO UPDATE SET
                          source=excluded.source,
                          snippet=excluded.snippet,
                          strength=excluded.strength,
                          retrieved_at=excluded.retrieved_at""",
                     (row["person_id"], "employment", row["url"], "apify_linkedin",
                      f"LinkedIn current position: {hit.title} at {hit.company}",
                      "strongest", utcnow()))
            if hit.title:
                # The scraped current-position title is first-party and newer than
                # the search-result title used at discovery time.  Always replace
                # the snippet title; S7 immediately re-runs the SOP role gate on it.
                ctx.db.x("UPDATE people SET title=? WHERE person_id=?",
                         (hit.title, row["person_id"]))
            confirmed += 1
        print(f"confirmed={confirmed}  employer_mismatch={mismatched}", flush=True)
        print("apify:", ctx.ledger.summary().get("apify"), flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    raise SystemExit(main(max(0, args.limit)))
