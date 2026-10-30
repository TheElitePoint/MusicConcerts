"""SQLite master datastore -- the permanent memory of all research.

Purpose (from the brief): never waste time, scraping or API calls re-researching
something already known. Every company, rejection, person, profile and score is
persisted forever, and `company_aliases` is consulted BEFORE any network call.

SQLite (not CSV) is the source of truth because duplicate control (SS52) needs real
uniqueness constraints, and REVIEW verdicts must survive across runs.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("data/master.sqlite")

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- companies
CREATE TABLE IF NOT EXISTS companies (
    company_id        INTEGER PRIMARY KEY,
    canonical_name    TEXT NOT NULL,
    name_normalized   TEXT NOT NULL,          -- SS52 strict key
    name_loose        TEXT,                   -- SS52 merge-candidate key
    domain            TEXT,                   -- SS52 primary identity
    website           TEXT,
    linkedin_url      TEXT,
    instagram_url     TEXT,
    country           TEXT,                   -- ISO-2, SS7
    country_confidence TEXT,                  -- 'verified' | 'single_source' | 'unknown'
    company_type      TEXT,                   -- key from sop.yaml company_types
    type_confidence   TEXT,
    roster_estimate   INTEGER,
    roster_low        INTEGER,
    roster_high       INTEGER,
    roster_display    TEXT,                   -- SS6 rendered string
    roster_evidence_url TEXT,
    parent_org        TEXT,                   -- SS9 affiliate carve-out
    mbid              TEXT,                   -- MusicBrainz label id (roster fallback)
    discogs_id        INTEGER,                -- Discogs label id (roster + recency)
    description       TEXT,                   -- the company's own words (SS8 gate input)
    status            TEXT NOT NULL DEFAULT 'NEW',
        -- NEW | RESOLVED | VERIFIED | QUALIFIED | REVIEW | REJECTED | UNRESOLVED
    gate_result       TEXT,                   -- JSON: per-gate PASS/FAIL/UNCLEAR
    first_seen        TEXT NOT NULL,
    last_researched   TEXT,
    discovered_via    TEXT,                   -- source that first surfaced it
    UNIQUE (domain)
);
CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name_normalized);
CREATE INDEX IF NOT EXISTS idx_companies_loose ON companies(name_loose);
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(status);

-- The skip index: consulted before ANY network call.
CREATE TABLE IF NOT EXISTS company_aliases (
    alias_normalized  TEXT PRIMARY KEY,
    company_id        INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    alias_raw         TEXT,
    source            TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_rejections (
    rejection_id  INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    gate          TEXT NOT NULL,              -- G1..G8
    reason_code   TEXT NOT NULL,
    reason_text   TEXT,
    rejected_at   TEXT NOT NULL,
    recheck_after TEXT                        -- NULL = permanent (e.g. hard exclusion)
);
CREATE INDEX IF NOT EXISTS idx_rej_company ON company_rejections(company_id);

CREATE TABLE IF NOT EXISTS company_evidence (
    evidence_id  INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,               -- roster|activity|type|country|profile|identity
    url          TEXT NOT NULL,
    source       TEXT NOT NULL,
    snippet      TEXT,
    strength     TEXT NOT NULL,               -- strongest|strong|weak  (SS38)
    retrieved_at TEXT NOT NULL,
    UNIQUE (company_id, kind, url)
);

CREATE TABLE IF NOT EXISTS activity_signals (
    signal_id     INTEGER PRIMARY KEY,
    company_id    INTEGER REFERENCES companies(company_id) ON DELETE CASCADE,
    raw_label     TEXT,                       -- pre-resolution label string
    signal_type   TEXT NOT NULL,              -- key from sop.yaml activity_signal_categories
    description   TEXT,
    evidence_url  TEXT,
    observed_at   TEXT,                       -- when the activity happened (release date etc.)
    recorded_at   TEXT NOT NULL,
    source        TEXT NOT NULL,
    external_id   TEXT,
    UNIQUE (source, external_id, signal_type)
);
CREATE INDEX IF NOT EXISTS idx_signals_company ON activity_signals(company_id);

CREATE TABLE IF NOT EXISTS artists (
    artist_id    INTEGER PRIMARY KEY,
    company_id   INTEGER REFERENCES companies(company_id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    name_normalized TEXT,
    mbid         TEXT,
    spotify_id   TEXT,
    relationship TEXT,                        -- label|management|distribution|services
    evidence_url TEXT,
    recorded_at  TEXT NOT NULL,
    UNIQUE (company_id, name_normalized)
);

-- ------------------------------------------------------------------ people
CREATE TABLE IF NOT EXISTS people (
    person_id        INTEGER PRIMARY KEY,
    company_id       INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    full_name        TEXT NOT NULL,
    name_normalized  TEXT NOT NULL,
    title            TEXT,
    title_family     TEXT,                    -- key from sop.yaml role_families
    seniority        TEXT,                    -- key from sop.yaml seniority_tiers
    role_relevance   INTEGER,
    current_verified INTEGER NOT NULL DEFAULT 0,
    employment_sources INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'CANDIDATE',
        -- CANDIDATE | VERIFIED | REVIEW | REJECTED
    reject_reason    TEXT,
    first_seen       TEXT NOT NULL,
    last_researched  TEXT,
    UNIQUE (company_id, name_normalized)
);
CREATE INDEX IF NOT EXISTS idx_people_status ON people(status);

CREATE TABLE IF NOT EXISTS profiles (
    profile_id   INTEGER PRIMARY KEY,
    person_id    INTEGER NOT NULL REFERENCES people(person_id) ON DELETE CASCADE,
    channel      TEXT NOT NULL,               -- linkedin | instagram
    url          TEXT NOT NULL,
    handle       TEXT,
    verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
        -- VERIFIED | UNVERIFIED | REJECTED
    identity_score INTEGER,                   -- SS35 signal total (instagram)
    usability    TEXT,                        -- full | partial | weak | none
    match_signals TEXT,                       -- JSON of which SS35 signals fired
    verified_at  TEXT,
    retrieved_at TEXT NOT NULL,
    UNIQUE (person_id, channel)
);
-- One person must never occupy two identities on the same channel (SS52).
CREATE UNIQUE INDEX IF NOT EXISTS idx_profiles_channel_url ON profiles(channel, url);

CREATE TABLE IF NOT EXISTS person_evidence (
    evidence_id  INTEGER PRIMARY KEY,
    person_id    INTEGER NOT NULL REFERENCES people(person_id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,               -- employment|title|identity|profile
    url          TEXT NOT NULL,
    source       TEXT NOT NULL,
    snippet      TEXT,
    strength     TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    UNIQUE (person_id, kind, url)
);

CREATE TABLE IF NOT EXISTS scores (
    score_id     INTEGER PRIMARY KEY,
    person_id    INTEGER NOT NULL REFERENCES people(person_id) ON DELETE CASCADE,
    channel      TEXT NOT NULL,               -- linkedin | instagram
    subscores    TEXT NOT NULL,               -- JSON {A1:10, A2:8, ...}
    total        INTEGER NOT NULL,
    verdict      TEXT NOT NULL,               -- FINAL PASS | RESEARCH FURTHER | NOT FINAL | REJECT
    failed_gates TEXT,                        -- JSON list, SS46
    why_company  TEXT,
    why_person   TEXT,
    sop_version  TEXT NOT NULL,
    scored_at    TEXT NOT NULL,
    UNIQUE (person_id, channel, sop_version)
);

-- --------------------------------------------------- human REVIEW verdicts
-- Persisted forever so an adjudicated item never re-surfaces (brief requirement).
CREATE TABLE IF NOT EXISTS review_verdicts (
    verdict_id   INTEGER PRIMARY KEY,
    entity_type  TEXT NOT NULL,               -- company | person
    entity_id    INTEGER NOT NULL,
    verdict      TEXT NOT NULL,               -- PASS | REJECT | RESEARCH_FURTHER
    reason       TEXT,
    decided_by   TEXT,                        -- 'human' | 'groq_suggested'
    decided_at   TEXT NOT NULL,
    UNIQUE (entity_type, entity_id, decided_by)
);

-- ------------------------------------------------------- runs & cost ledger
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    config_hash  TEXT,
    params       TEXT,
    stats        TEXT
);

CREATE TABLE IF NOT EXISTS api_calls (
    call_id      INTEGER PRIMARY KEY,
    run_id       TEXT,
    provider     TEXT NOT NULL,
    endpoint     TEXT,
    cache_hit    INTEGER NOT NULL DEFAULT 0,
    cost         REAL NOT NULL DEFAULT 0.0,
    status       INTEGER,
    called_at    TEXT NOT NULL,
    context      TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_provider ON api_calls(provider, called_at);

-- When a person FIRST reached a final sheet, so each export can report what is new
-- rather than re-listing the whole sheet. Survives restarts because it lives here
-- rather than in a run directory.
CREATE TABLE IF NOT EXISTS sheet_membership (
    person_id   INTEGER NOT NULL REFERENCES people(person_id) ON DELETE CASCADE,
    channel     TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    batch       TEXT NOT NULL,
    PRIMARY KEY (person_id, channel)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thin SQLite wrapper. All stages share one instance per run."""

    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A generous busy timeout lets concurrent stages (e.g. company growth and
        # people discovery) share the database instead of failing on lock contention.
        self.conn = sqlite3.connect(self.path, timeout=60.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive column migrations for databases created by an earlier version."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(companies)")}
        for column, ddl in (("mbid", "TEXT"), ("discogs_id", "INTEGER"),
                            ("description", "TEXT")):
            if column not in have:
                self.conn.execute(f"ALTER TABLE companies ADD COLUMN {column} {ddl}")
        self.conn.commit()

    # -- primitives --------------------------------------------------------
    def q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def x(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def close(self) -> None:
        self.conn.close()

    # -- SS52 identity resolution -----------------------------------------
    def find_company_by_keys(self, keys: list[tuple[str, str]]) -> int | None:
        """Walk SS52 keys strongest-first; return the first company_id that matches."""
        col = {"domain": "domain", "instagram": "instagram_url",
               "linkedin": "linkedin_url", "name": "name_normalized"}
        for kind, value in keys:
            if not value:
                continue
            if kind == "name":
                row = self.one(
                    "SELECT company_id FROM companies WHERE name_normalized = ?", (value,))
            elif kind == "domain":
                row = self.one("SELECT company_id FROM companies WHERE domain = ?", (value,))
            else:
                row = self.one(
                    f"SELECT company_id FROM companies WHERE {col[kind]} LIKE ?",
                    (f"%{value}%",))
            if row:
                return row["company_id"]
        return None

    def alias_lookup(self, alias_normalized: str) -> int | None:
        """The pre-network skip index."""
        row = self.one(
            "SELECT company_id FROM company_aliases WHERE alias_normalized = ?",
            (alias_normalized,))
        return row["company_id"] if row else None

    def add_alias(self, alias_normalized: str, company_id: int,
                  alias_raw: str = "", source: str = "") -> None:
        self.x("""INSERT OR IGNORE INTO company_aliases
                  (alias_normalized, company_id, alias_raw, source, created_at)
                  VALUES (?,?,?,?,?)""",
               (alias_normalized, company_id, alias_raw, source, utcnow()))

    def should_skip_company(self, company_id: int, revisit_days: int = 30) -> tuple[bool, str]:
        """True when prior research already settled this company.

        Rejections with no recheck_after are permanent (hard exclusions, excluded brands).
        """
        row = self.one("SELECT status, last_researched FROM companies WHERE company_id = ?",
                       (company_id,))
        if not row:
            return False, ""
        if row["status"] == "REJECTED":
            rej = self.one("""SELECT reason_text, recheck_after FROM company_rejections
                              WHERE company_id = ? ORDER BY rejected_at DESC LIMIT 1""",
                           (company_id,))
            if rej and not rej["recheck_after"]:
                return True, f"previously rejected: {rej['reason_text']}"
            if rej and rej["recheck_after"] > utcnow():
                return True, f"rejected, recheck after {rej['recheck_after']}"
        if row["status"] in ("VERIFIED", "QUALIFIED") and row["last_researched"]:
            researched = datetime.fromisoformat(row["last_researched"])
            if researched.tzinfo is None:      # ad-hoc scripts wrote naive UTC stamps
                researched = researched.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - researched).days
            if age_days < revisit_days:
                return True, f"researched {age_days}d ago"
        return False, ""

    # -- ledger ------------------------------------------------------------
    def log_call(self, provider: str, endpoint: str = "", *, cache_hit: bool = False,
                 cost: float = 0.0, status: int | None = None,
                 run_id: str = "", context: str = "") -> None:
        self.x("""INSERT INTO api_calls
                  (run_id, provider, endpoint, cache_hit, cost, status, called_at, context)
                  VALUES (?,?,?,?,?,?,?,?)""",
               (run_id, provider, endpoint, int(cache_hit), cost, status, utcnow(), context))

    def spend_since(self, provider: str, since_iso: str) -> tuple[int, float]:
        row = self.one("""SELECT COUNT(*) n, COALESCE(SUM(cost),0) c FROM api_calls
                          WHERE provider = ? AND cache_hit = 0 AND called_at >= ?""",
                       (provider, since_iso))
        return (row["n"], row["c"]) if row else (0, 0.0)

    @staticmethod
    def js(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
