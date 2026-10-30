"""Shared run context passed through every stage."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .budget import BudgetLedger
from .db import Database
from .http import HttpClient


@dataclass
class Context:
    db: Database
    ledger: BudgetLedger
    http: HttpClient
    run_id: str
    run_dir: Path
    countries: list[str] = field(default_factory=list)
    limit: int = 0
    stats: dict = field(default_factory=dict)

    @classmethod
    def create(cls, *, countries: list[str] | None = None, limit: int = 0,
               db_path: str | Path | None = None,
               runs_root: str | Path = "runs") -> "Context":
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        run_dir = Path(runs_root) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        db = Database(db_path or "data/master.sqlite")
        ledger = BudgetLedger(db, run_id=run_id)
        http = HttpClient(ledger)

        db.x("INSERT INTO runs (run_id, started_at, config_hash, params) VALUES (?,?,?,?)",
             (run_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
              config.config_hash(),
              db.js({"countries": countries or [], "limit": limit})))

        return cls(db=db, ledger=ledger, http=http, run_id=run_id, run_dir=run_dir,
                   countries=countries or [], limit=limit)

    def bump(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def finish(self) -> None:
        self.db.x("UPDATE runs SET finished_at = ?, stats = ? WHERE run_id = ?",
                  (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   self.db.js({"stats": self.stats, "budget": self.ledger.summary()}),
                   self.run_id))

    def close(self) -> None:
        self.finish()
        self.db.close()
