"""Master exclusion list of already-used contacts and their companies.

Built by scripts/build_used_contacts_block.py from the client's "Used Data"
workbook. A blocked company is never searched for people; a blocked person is
never profile-resolved or exported. Absent file -> nothing is blocked.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

from .normalize import linkedin_slug, normalize_company_name, normalize_person_name

BLOCK_FILE = Path(__file__).resolve().parents[2] / "data/reference/used_contacts_block.json"


def slug_key(url: str) -> str:
    kind, slug = linkedin_slug(unquote(str(url or "")))
    return slug.lower() if kind == "in" else ""


@lru_cache(maxsize=1)
def _block() -> dict[str, frozenset]:
    if not BLOCK_FILE.exists():
        return {"slugs": frozenset(), "names": frozenset(), "companies": frozenset()}
    data = json.loads(BLOCK_FILE.read_text(encoding="utf-8"))
    return {k: frozenset(data.get(k, [])) for k in ("slugs", "names", "companies")}


def company_blocked(name: str) -> bool:
    key = normalize_company_name(name or "")
    return bool(key) and key in _block()["companies"]


def person_blocked(name: str = "", linkedin_url: str = "") -> bool:
    slug = slug_key(linkedin_url)
    if slug and slug in _block()["slugs"]:
        return True
    key = normalize_person_name(name or "")
    return bool(key) and key in _block()["names"]
