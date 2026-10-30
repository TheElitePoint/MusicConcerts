"""Build data/reference/used_contacts_block.json from the "Used Data" workbook."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.normalize import normalize_company_name, normalize_person_name  # noqa: E402
from mp.used_contacts import BLOCK_FILE, slug_key  # noqa: E402

# Employer strings that are not a company and must not block one by accident.
NOT_A_COMPANY = {"", "freelance", "freelancer", "self employed", "selfemployed", "independent",
                 "autonomo", "independiente", "none", "n a", "na", "confidential", "stealth"}


def main(path: str) -> int:
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = next(i for i, r in enumerate(rows) if r and r[0] == "Person Name")
    col = {name: i for i, name in enumerate(rows[head]) if name}
    slugs, names, companies = set(), set(), set()
    for r in rows[head + 1:]:
        if not r or not any(r):
            continue
        get = lambda c: str(r[col[c]] or "").strip() if col[c] < len(r) else ""  # noqa: E731
        name = normalize_person_name(get("Person Name"))
        if len(name.split()) >= 2:          # a single token is too ambiguous to block on
            names.add(name)
        for url in get("LinkedIn URL(s)").split("|"):
            if slug_key(url):
                slugs.add(slug_key(url))
        key = get("Dedup Search Key")
        if key.startswith("linkedin.com/in/") and slug_key("https://" + key):
            slugs.add(slug_key("https://" + key))
        company = normalize_company_name(get("Company"))
        if company not in NOT_A_COMPANY and len(company) >= 3:
            companies.add(company)
    BLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    BLOCK_FILE.write_text(json.dumps({"source": Path(path).name, "slugs": sorted(slugs),
                                      "names": sorted(names), "companies": sorted(companies)},
                                     ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"slugs": len(slugs), "names": len(names), "companies": len(companies)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
