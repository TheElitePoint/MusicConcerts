"""API budget protection: ledger, caps and circuit breakers.

Governing rules from the brief:
  * API usage must be extremely conservative.
  * Never burn credits when the same information is obtainable for free.
  * Tavily is an opportunistic second layer -- the pipeline must never depend on it.

When a cap is reached the item becomes REVIEW. It is never guessed and never crashes (SS55).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import config
from .db import Database
from .keys import QUOTA_STATUSES, KeyRegistry


class BudgetExceeded(Exception):
    """Raised when a call would breach a cap. Callers degrade to REVIEW."""


class PreconditionFailed(Exception):
    """Raised when a metered call is attempted without its required preconditions.

    This is a programming error, not a runtime condition -- it means paid budget
    was about to leak into a stage that must stay free (e.g. Apify in discovery).
    """


@dataclass
class ProviderState:
    calls: int = 0
    cost: float = 0.0
    cache_hits: int = 0
    consecutive_failures: int = 0
    rotations: int = 0
    tripped: bool = False
    trip_reason: str = ""


@dataclass
class BudgetLedger:
    db: Database
    run_id: str = ""
    state: dict[str, ProviderState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._cfg = config.budget()["providers"]
        self.keyring = KeyRegistry()

    def key_for(self, provider: str) -> str:
        """The provider's currently active credential ('' when exhausted)."""
        return self.keyring.current(provider)

    def _p(self, provider: str) -> ProviderState:
        return self.state.setdefault(provider, ProviderState())

    def spec(self, provider: str) -> dict:
        return self._cfg.get(provider, {})

    # -- gate --------------------------------------------------------------
    def check(self, provider: str, *, cost: float | None = None) -> None:
        """Raise BudgetExceeded if this call is not permitted. Call BEFORE dispatch."""
        spec = self.spec(provider)
        st = self._p(provider)

        if st.tripped:
            raise BudgetExceeded(f"{provider}: circuit breaker open ({st.trip_reason})")

        per_run = spec.get("per_run")
        if per_run is not None and st.calls >= per_run:
            raise BudgetExceeded(f"{provider}: per-run cap {per_run} reached")

        per_month = spec.get("per_month")
        if per_month is not None:
            since = datetime.now(timezone.utc).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
            n, _ = self.db.spend_since(provider, since)
            if n + st.calls >= per_month:
                raise BudgetExceeded(f"{provider}: per-month cap {per_month} reached")

    def is_available(self, provider: str) -> bool:
        """Non-raising form -- used to decide whether to even attempt an optional layer."""
        if not config.has_key(provider) and provider in ("spotify", "discogs", "groq",
                                                         "apify", "tavily"):
            return False
        if self.keyring.ring(provider).exhausted:
            return False
        try:
            self.check(provider)
            return True
        except BudgetExceeded:
            return False

    # -- record ------------------------------------------------------------
    def record(self, provider: str, endpoint: str = "", *, cache_hit: bool = False,
               status: int | None = None, context: str = "") -> None:
        st = self._p(provider)
        spec = self.spec(provider)
        if cache_hit:
            st.cache_hits += 1
            self.db.log_call(provider, endpoint, cache_hit=True, cost=0.0,
                             status=status, run_id=self.run_id, context=context)
            return

        # Prefer the actor-specific price when the endpoint identifies one, so the
        # ledger's dollar total reflects reality rather than a flat guess.
        cost = float(spec.get("cost_per_call", 0.0))
        by_actor = spec.get("cost_by_actor") or {}
        for actor, price in by_actor.items():
            if actor.replace("/", "~") in (endpoint or "") or actor in (endpoint or ""):
                cost = float(price)
                break
        st.calls += 1
        st.cost += cost
        self.db.log_call(provider, endpoint, cache_hit=False, cost=cost,
                         status=status, run_id=self.run_id, context=context)

        # Quota/auth failure: retire this key and try the next one. Only when the
        # whole ring is exhausted does the provider actually go down.
        trip_statuses = spec.get("trip_on_status") or []
        if status in QUOTA_STATUSES and self.keyring.ring(provider).keys:
            if self.keyring.rotate(provider, f"HTTP {status}"):
                st.consecutive_failures = 0
                st.rotations += 1
                return
            self.trip(provider, f"all {len(self.keyring.ring(provider).keys)} "
                                f"key(s) exhausted (HTTP {status})")
            return
        if status in trip_statuses:
            self.trip(provider, f"HTTP {status}")
            return
        # status == 0 means the transport itself failed (timeout, connection reset --
        # see http.py's `except requests.RequestException`), not a server response.
        # That must count toward consecutive_failures like any other failure: a
        # provider that has started hanging rather than erroring would otherwise hit
        # `else: st.consecutive_failures = 0` below on every attempt, so a
        # trip_after_consecutive_failures breaker configured for it would never
        # fire and every remaining call in the run pays the full timeout for nothing.
        if status == 0 or (status is not None and status >= 400):
            st.consecutive_failures += 1
            limit = spec.get("trip_after_consecutive_failures")
            if limit and st.consecutive_failures >= limit:
                self.trip(provider, f"{limit} consecutive failures")
        else:
            st.consecutive_failures = 0

    def trip(self, provider: str, reason: str) -> None:
        st = self._p(provider)
        st.tripped = True
        st.trip_reason = reason

    # -- preconditions -----------------------------------------------------
    def require(self, provider: str, conditions: dict[str, bool]) -> None:
        """Assert every declared precondition for a metered provider holds.

        `conditions` maps the names in budget.yaml to the caller's evaluation.
        A missing or False condition raises -- budget cannot leak silently.
        """
        declared = self.spec(provider).get("preconditions") or []
        for name in declared:
            if not conditions.get(name, False):
                raise PreconditionFailed(
                    f"{provider}: precondition '{name}' not satisfied -- refusing call")

    # -- reporting ---------------------------------------------------------
    def summary(self) -> dict[str, dict]:
        out = {}
        for provider, st in sorted(self.state.items()):
            total = st.calls + st.cache_hits
            out[provider] = {
                "calls": st.calls,
                "cache_hits": st.cache_hits,
                "cache_rate": round(st.cache_hits / total, 3) if total else 0.0,
                "cost": round(st.cost, 4),
                "rotations": st.rotations,
                "tripped": st.tripped,
                "trip_reason": st.trip_reason,
            }
        return out

    def total_cost(self) -> float:
        return round(sum(s.cost for s in self.state.values()), 4)
