"""Print non-secret monthly usage capacity for configured Apify accounts."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config  # noqa: E402


def main() -> int:
    supplied = os.environ.get("APIFY_TOKEN", "").strip()
    tokens = ([supplied] if supplied else []) + [
        token for token in config.keys("apify") if token != supplied
    ]
    for index, token in enumerate(tokens, 1):
        try:
            response = requests.get(
                "https://api.apify.com/v2/users/me/limits",
                headers={"Authorization": f"Bearer {token}"}, timeout=30,
            )
            response.raise_for_status()
            data = response.json().get("data") or {}
            cap = float((data.get("limits") or {}).get("maxMonthlyUsageUsd") or 0)
            used = float((data.get("current") or {}).get("monthlyUsageUsd") or 0)
            print(f"account={index} cap=${cap:.2f} used=${used:.2f} remaining=${max(0, cap-used):.2f}")
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" status={status}" if status is not None else ""
            print(f"account={index} error={type(exc).__name__}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
