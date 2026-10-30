"""Config loading. All research policy lives in config/*.yaml, never in code."""
from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"

load_dotenv(ROOT / ".env")


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    """Load and cache config/<name>.yaml."""
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"missing config: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def sop() -> dict:
    return load("sop")


def scoring() -> dict:
    return load("scoring")


def budget() -> dict:
    return load("budget")


def sources() -> dict:
    return load("sources")


def config_hash() -> str:
    """Fingerprint of all config -- stored on each run so results stay traceable."""
    h = hashlib.sha256()
    for name in sorted(("sop", "scoring", "budget", "sources")):
        h.update((CONFIG_DIR / f"{name}.yaml").read_bytes())
    return h.hexdigest()[:12]


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default) or default


# Providers whose credentials may be supplied as a comma-separated rotation list.
# Both the plural and singular env names are accepted.
_KEY_VARS = {
    "groq":   ("GROQ_API_KEYS", "GROQ_API_KEY"),
    "apify":  ("APIFY_TOKENS", "APIFY_TOKEN"),
    "tavily": ("TAVILY_API_KEYS", "TAVILY_API_KEY"),
    "discogs": ("DISCOGS_TOKENS", "DISCOGS_TOKEN"),
}


def keys(provider: str) -> list[str]:
    """All credentials for a provider, in rotation order. [] when unconfigured."""
    out: list[str] = []
    for var in _KEY_VARS.get(provider, ()):
        for raw in env(var).split(","):
            key = raw.strip().strip('"\'')
            if key and key not in out:
                out.append(key)
    return out


def has_key(provider: str) -> bool:
    """Whether credentials for an optional provider are present."""
    if provider == "spotify":
        return bool(env("SPOTIFY_CLIENT_ID") and env("SPOTIFY_CLIENT_SECRET"))
    return bool(keys(provider))


@lru_cache(maxsize=None)
def approved_countries() -> dict[str, str]:
    """Flatten SS7 geography into {ISO2: name}."""
    out: dict[str, str] = {}
    for region in sop()["geography"]["approved"].values():
        out.update(region)
    return out


@lru_cache(maxsize=None)
def country_name_to_iso() -> dict[str, str]:
    return {name.lower(): iso for iso, name in approved_countries().items()}


def user_agent() -> str:
    """MusicBrainz API policy requires a contact-bearing User-Agent."""
    contact = env("MB_CONTACT_EMAIL", "research@example.com")
    return f"MusicProspectResearch/0.1 ( {contact} )"
