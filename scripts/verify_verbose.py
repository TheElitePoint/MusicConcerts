"""Employment verification with explicit account selection and step-by-step logging.

Written after several silent failures: the default account was over its monthly
dollar cap, so every batch returned nothing while the ledger reported only cache
hits. This picks an account that still has budget AND allowance, and prints what
happens at each step so a failure is visible rather than silent.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests                                        # noqa: E402

from mp import config                                  # noqa: E402
from mp.db import utcnow                                # noqa: E402
from mp.normalize import normalize_company_name, normalize_person_name  # noqa: E402
from mp.pipeline import Context                         # noqa: E402
from mp.sources import apify                            # noqa: E402

BATCH_ACTOR_ID = "LpVuK3Zozwuipa5bp"
COND = {"company_passed_all_gates": True, "person_has_verified_name": True,
        "person_title_in_role_family": True, "no_cached_profile": True,
        "free_sources_exhausted": True}


def funded_accounts() -> list[int]:
    """Indices of accounts with BOTH remaining budget and actor allowance."""
    out = []
    for i, tok in enumerate(config.keys("apify")):
        try:
            lim = requests.get("https://api.apify.com/v2/users/me/limits",
                               params={"token": tok}, timeout=20).json().get("data", {})
        except requests.RequestException:
            continue
        used = (lim.get("current") or {}).get("monthlyUsageUsd", 0)
        cap = lim.get("limits", {}).get("maxMonthlyUsageUsd", 0)
        if cap - used <= 0.25:
            print(f"  account {i}: SKIP (${used:.2f}/${cap} - no budget)")
            continue
        if apify._hit_actor_run_limit(tok, BATCH_ACTOR_ID):
            print(f"  account {i}: SKIP (actor capped)")
            continue
        print(f"  account {i}: usable (${cap - used:.2f} left)")
        out.append(i)
    return out


def main() -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        print("account audit:")
        accounts = funded_accounts()
        if not accounts:
            print("no usable account; aborting")
            return 1

        rows = ctx.db.q("""SELECT p.person_id, p.full_name, c.canonical_name AS company,
                                  pr.url
                           FROM people p
                           JOIN companies c ON c.company_id = p.company_id
                           JOIN profiles pr ON pr.person_id = p.person_id
                                           AND pr.channel='linkedin'
                           WHERE c.status='QUALIFIED' AND p.current_verified = 0
                             AND p.status != 'REJECTED'""")
        print(f"\n{len(rows)} people need first-party employment evidence")

        confirmed = mismatch = unmatched = 0
        acct = 0
        for start in range(0, len(rows), 10):
            chunk = rows[start:start + 10]
            ctx.ledger.keyring.ring("apify").index = accounts[acct % len(accounts)]
            acct += 1
            hits = apify.fetch_profiles_batch(
                ctx.http, [r["url"] for r in chunk], conditions=COND)
            by_name = {}
            for hit in hits.values():
                if hit.full_name:
                    by_name.setdefault(normalize_person_name(hit.full_name), hit)
            print(f"  chunk {start // 10 + 1}: sent {len(chunk)} -> "
                  f"{len(hits)} records, {len(by_name)} distinct names")

            for row in chunk:
                url = (row["url"] or "").lower()
                slug = url.rstrip("/").rsplit("/", 1)[-1]
                hit = (hits.get(url) or hits.get(slug)
                       or by_name.get(normalize_person_name(row["full_name"])))
                if not hit or not hit.company:
                    unmatched += 1
                    continue
                expected = normalize_company_name(row["company"])
                found = normalize_company_name(hit.company)
                if expected and found and not (expected in found or found in expected):
                    ctx.db.x("""UPDATE people SET status='REJECTED', reject_reason=?,
                                last_researched=? WHERE person_id=?""",
                             (f"SS34 employer mismatch: LinkedIn shows '{hit.company}', "
                              f"not '{row['company']}'", utcnow(), row["person_id"]))
                    mismatch += 1
                    continue
                ctx.db.x("""INSERT OR IGNORE INTO person_evidence
                            (person_id, kind, url, source, snippet, strength, retrieved_at)
                            VALUES (?,?,?,?,?,?,?)""",
                         (row["person_id"], "employment", row["url"], "apify_linkedin",
                          f"LinkedIn current position: {hit.title} at {hit.company}",
                          "strongest", utcnow()))
                if hit.title:
                    ctx.db.x("""UPDATE people SET title=COALESCE(NULLIF(title,''),?)
                                WHERE person_id=?""", (hit.title, row["person_id"]))
                confirmed += 1

        print(f"\nconfirmed={confirmed}  employer_mismatch={mismatch}  unmatched={unmatched}")
        print("apify:", ctx.ledger.summary().get("apify"))
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
