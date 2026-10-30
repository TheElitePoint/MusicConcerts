"""Continuous research loop: discover -> resolve -> verify -> gate -> people -> export.

Design notes
------------
* Countries rotate across the full SS7 approved list, so each cycle explores new
  territory instead of re-walking the same market.
* The alias skip-index means re-encountering a known company costs nothing, so
  overlapping sweeps are cheap by design.
* Metered people discovery is SKIPPED automatically when every Apify account is
  capped for the actor. The free stages keep running, so the queue of qualified
  companies with LinkedIn URLs keeps growing and is ready the moment capacity
  returns (new month, or a new account).
* Every cycle exports, so there is always a current workbook on disk.
* Fully resumable: state lives in SQLite and each stage is idempotent.
"""
from __future__ import annotations

import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config                                              # noqa: E402
from mp.pipeline import Context                                    # noqa: E402
from mp.sources import apify                                       # noqa: E402
from mp.stages import (s1_discovery, s2_resolve, s3_verify,        # noqa: E402
                       s4_gates, s5_people, s6_profiles,
                       s7_person_verify, s8_score, s9_export)
from mp.stages.s3_verify import _find_company_linkedin             # noqa: E402
from mp.db import Database, utcnow                                 # noqa: E402

# SS7 approved countries, ordered so the densest independent-label markets come
# first and the loop starts producing early.
COUNTRY_ORDER = ["GB", "US", "DE", "NL", "SE", "FR", "CA", "AU", "IE", "ES",
                 "IT", "NO", "DK", "FI", "BE", "AT", "CH", "PT", "PL", "NZ",
                 "JP", "BR", "MX", "CZ", "GR", "IS", "EE", "SI", "HR", "LU",
                 "SG", "KR", "CL"]
BATCH = 3          # countries per discovery cycle
PAUSE_SECONDS = 30

# --- balance guards -------------------------------------------------------
# Discovery is cheap and people discovery is metered, so an unguarded loop piles
# up thousands of companies that can never be worked. These keep the funnel in
# proportion: stop discovering while a usable backlog already exists, and stop
# resolving LinkedIn URLs once enough targets are queued.
BACKLOG_READY_LIMIT = 40      # qualified + LinkedIn URL + no people yet
UNQUALIFIED_BACKLOG_LIMIT = 250   # resolved/verified but not yet gated
TARGET_PEOPLE_PER_COMPANY = 6     # SS51 mid-point of the 4-10 band


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def people_search_available() -> bool:
    """Whether any account still has allowance AND money for a company people-search."""
    for token in config.keys("apify"):
        if apify.account_budget(token, refresh=True) < 0.25:
            continue
        for actor_id in ("JbJke4iGh7hRR5eBb", "M2FMdjRVeF1HPGFcc"):
            if not apify._hit_actor_run_limit(token, actor_id):
                return True
    return False


def profile_batch_available() -> bool:
    """Whether any account can still fetch profiles for employment verification.

    This allowance is SEPARATE from people-search. Gating verification on
    people-search availability (as this loop first did) strands it: the loop kept
    discovering people from free sources but never upgraded their evidence, so rows
    stayed flat while the people count climbed.
    """
    if not config.sources()["profiles"].get("paid_profile_verification", True):
        return False
    return any(apify.account_budget(token, refresh=True) >= 0.25
               and not apify._hit_actor_run_limit(token, "LpVuK3Zozwuipa5bp")
               for token in config.keys("apify"))


def backfill_linkedin(ctx: Context, limit: int = 40) -> int:
    """Resolve LinkedIn company URLs for qualified companies (free, via search)."""
    rows = ctx.db.q("""SELECT company_id, canonical_name FROM companies
                       WHERE status='QUALIFIED' AND linkedin_url IS NULL
                       ORDER BY roster_estimate DESC LIMIT ?""", (limit,))
    found = 0
    for row in rows:
        try:
            url = _find_company_linkedin(ctx, row["canonical_name"])
        except Exception:
            continue
        if url:
            ctx.db.x("""UPDATE companies SET linkedin_url=?, last_researched=?
                        WHERE company_id=?""", (url, utcnow(), row["company_id"]))
            found += 1
    return found


def backlog(db: Database) -> dict:
    """How much unworked capacity already exists at each funnel stage."""
    one = lambda q: db.one(q)["n"]  # noqa: E731
    return {
        "ready": one("""SELECT COUNT(*) n FROM companies c WHERE status='QUALIFIED'
                        AND linkedin_url IS NOT NULL
                        AND NOT EXISTS (SELECT 1 FROM people p
                                        WHERE p.company_id=c.company_id)"""),
        "ungated": one("SELECT COUNT(*) n FROM companies "
                       "WHERE status IN ('NEW','RESOLVED','VERIFIED')"),
        "no_li": one("""SELECT COUNT(*) n FROM companies WHERE status='QUALIFIED'
                        AND linkedin_url IS NULL"""),
    }


def snapshot() -> dict:
    db = Database("data/master.sqlite")
    try:
        one = lambda q: db.one(q)["n"]  # noqa: E731
        people = one("SELECT COUNT(*) n FROM people")
        with_people = one("SELECT COUNT(DISTINCT company_id) n FROM people")
        return {
            "companies": one("SELECT COUNT(*) n FROM companies"),
            "ppl_per_co": round(people / with_people, 1) if with_people else 0.0,
            "qualified": one("SELECT COUNT(*) n FROM companies WHERE status='QUALIFIED'"),
            "with_li": one("SELECT COUNT(*) n FROM companies WHERE status='QUALIFIED' "
                           "AND linkedin_url IS NOT NULL"),
            "people": one("SELECT COUNT(*) n FROM people"),
            "verified": one("SELECT COUNT(*) n FROM people WHERE current_verified=1"),
            "rows": one("SELECT COUNT(*) n FROM scores WHERE verdict='FINAL PASS'"),
        }
    finally:
        db.close()


def write_workbook() -> None:
    """Refresh the main workbook AND today's new-people file. Writes are atomic."""
    import os as _os
    import subprocess
    env = {**_os.environ, "PYTHONPATH": "src"}
    subprocess.run([sys.executable, "scripts/export_workbook.py"], cwd=str(ROOT),
                   env=env, capture_output=True)
    # Daily delta: "new data/data_<date>.xlsx". Must run AFTER the main export,
    # which is what records sheet membership.
    subprocess.run([sys.executable, "scripts/export_new_data.py"], cwd=str(ROOT),
                   env=env, capture_output=True)


def backup_database() -> None:
    """Point-in-time SQLite backup before each cycle.

    The loop is safe to kill at any moment -- SQLite is in WAL mode and every stage
    is idempotent -- but a rolling backup also protects against disk-level damage
    and makes it trivial to roll back an unwanted change.
    """
    import shutil as _shutil
    src = ROOT / "data" / "master.sqlite"
    if not src.exists():
        return
    dest_dir = ROOT / "data" / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    db = Database(src)          # checkpoint WAL so the copy is complete
    try:
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
    _shutil.copy2(src, dest_dir / f"master_{stamp}.sqlite")
    snaps = sorted(dest_dir.glob("master_*.sqlite"))
    for old in snaps[:-10]:     # keep the 10 most recent
        old.unlink()


def cycle(index: int) -> None:
    start = (index * BATCH) % len(COUNTRY_ORDER)
    countries = [COUNTRY_ORDER[(start + i) % len(COUNTRY_ORDER)] for i in range(BATCH)]
    metered = people_search_available()

    db = Database("data/master.sqlite")
    try:
        bl = backlog(db)
    finally:
        db.close()

    # Only widen the funnel when the existing backlog is actually workable.
    do_discover = bl["ready"] < BACKLOG_READY_LIMIT and bl["ungated"] < UNQUALIFIED_BACKLOG_LIMIT
    do_backfill = bl["ready"] < BACKLOG_READY_LIMIT

    log(f"cycle {index}: countries={','.join(countries)} "
        f"people_search={'AVAILABLE' if metered else 'capped'} "
        f"verify={'AVAILABLE' if profile_batch_available() else 'capped'} "
        f"backlog(ready={bl['ready']}, ungated={bl['ungated']}, no_li={bl['no_li']}) "
        f"discover={'yes' if do_discover else 'SKIP (backlog full)'}")

    backup_database()

    ctx = Context.create(countries=countries, db_path="data/master.sqlite")
    try:
        if do_discover:
            log(f"  discover  {s1_discovery.run(ctx, lookback_days=150)}")
        else:
            log("  discover  skipped - converting existing backlog first")
        log(f"  resolve   {s2_resolve.run(ctx, limit=120)}")
        log(f"  verify    {s3_verify.run(ctx, limit=120)}")
        log(f"  gate      {s4_gates.run(ctx)}")
        write_workbook()          # companies tab refreshed mid-cycle
        if do_backfill:
            log(f"  linkedin  backfilled {backfill_linkedin(ctx)} company URLs")
        else:
            log("  linkedin  skipped - enough targets already queued")
        if metered:
            log(f"  people    {s5_people.run(ctx)}")
        log(f"  profiles  {s6_profiles.run(ctx)}")
        log(f"  verifyppl {s7_person_verify.run(ctx)}")
        log(f"  score     {s8_score.run(ctx)}")
        log(f"  export    {s9_export.run(ctx)}")
        write_workbook()          # people tabs refreshed
    finally:
        ctx.close()

    # Verification runs on its OWN allowance, independent of people-search.
    if profile_batch_available():
        try:
            import subprocess
            subprocess.run([sys.executable, "scripts/verify_profiles_batch.py"],
                           cwd=str(ROOT), timeout=1800,
                           env={**__import__("os").environ, "PYTHONPATH": "src"},
                           check=False)
            log("  batch-verify complete")
        except Exception as exc:
            log(f"  batch-verify skipped: {exc}")
        # Re-score so newly verified people reach the sheets this cycle.
        ctx2 = Context.create(countries=countries, db_path="data/master.sqlite")
        try:
            log(f"  rescore   {s7_person_verify.run(ctx2)}")
            log(f"  reexport  {s9_export.run(ctx2)}")
        finally:
            ctx2.close()

    write_workbook()
    log(f"  STATE {snapshot()}")


PID_FILE = ROOT / "data" / "loop.pid"


def main() -> int:
    # A PID file makes the loop addressable precisely. Matching on the script name
    # with pkill -f is unsafe: it also matches monitor scripts that merely MENTION
    # the name in a pgrep argument, silently killing them too.
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(__import__("os").getpid()))
    log(f"loop starting (pid {__import__('os').getpid()}); "
        f"{len(COUNTRY_ORDER)} countries, {BATCH} per cycle")
    index = 0
    while True:
        try:
            cycle(index)
        except KeyboardInterrupt:
            log("interrupted")
            return 0
        except Exception:
            log("cycle failed:\n" + traceback.format_exc()[-1200:])
        index += 1
        time.sleep(PAUSE_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
