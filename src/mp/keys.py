"""Multi-key rotation.

The credentials for this project are supplied as comma-separated lists
(`TAVILY_API_KEYS`, `GROQ_API_KEYS`, `APIFY_TOKENS`). Rotation matters most for
Tavily: five free-tier keys are ~5x the monthly quota, but only if an exhausted key
advances to the next one instead of disabling the provider for the whole run.

Rotation applies ONLY to quota/auth errors (401/402/403/429). A 500 from the
provider is not a key problem and must not burn through the ring.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Statuses that mean "this KEY is finished", as opposed to "this REQUEST failed".
QUOTA_STATUSES = frozenset({401, 402, 403, 429, 432, 433})


@dataclass
class KeyRing:
    provider: str
    keys: list[str] = field(default_factory=list)
    index: int = 0
    retired: set[int] = field(default_factory=set)

    @property
    def available(self) -> bool:
        return bool(self.keys) and len(self.retired) < len(self.keys)

    @property
    def exhausted(self) -> bool:
        return bool(self.keys) and len(self.retired) >= len(self.keys)

    def current(self) -> str:
        """The active key, or '' when none remain."""
        if not self.available:
            return ""
        for offset in range(len(self.keys)):
            i = (self.index + offset) % len(self.keys)
            if i not in self.retired:
                self.index = i
                return self.keys[i]
        return ""

    def rotate(self, reason: str = "") -> bool:
        """Retire the current key and advance. True if another key is available."""
        if not self.keys:
            return False
        self.retired.add(self.index)
        self.index = (self.index + 1) % len(self.keys)
        return self.available

    def status(self) -> dict:
        return {"total": len(self.keys), "retired": len(self.retired),
                "active_index": self.index, "available": self.available}


class KeyRegistry:
    """Per-run holder of every provider's key ring."""

    def __init__(self) -> None:
        self._rings: dict[str, KeyRing] = {}

    def ring(self, provider: str) -> KeyRing:
        if provider not in self._rings:
            from . import config
            self._rings[provider] = KeyRing(provider, config.keys(provider))
        return self._rings[provider]

    def current(self, provider: str) -> str:
        return self.ring(provider).current()

    def rotate(self, provider: str, reason: str = "") -> bool:
        return self.ring(provider).rotate(reason)

    def summary(self) -> dict[str, dict]:
        return {p: r.status() for p, r in self._rings.items() if r.keys}
