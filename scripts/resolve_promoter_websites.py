"""Find official websites for promoter companies (free web search only).

"Company Website" is a mandatory sheet column, and Ticketmaster promoter records
carry no URL. A result is accepted only when its registrable domain closely matches
the promoter's name (so ticket sellers, venues and listings sites never qualify).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.db import utcnow  # noqa: E402
from mp.normalize import loose_company_key, registrable_domain  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.sources import websearch  # noqa: E402

NOT_OFFICIAL = ("ticketmaster", "livenation", "eventbrite", "dice.fm", "songkick", "bandsintown",
                "facebook", "instagram", "linkedin", "twitter", "x.com", "youtube", "wikipedia",
                "seetickets", "axs.com", "eventim", "residentadvisor", "ra.co", "skiddle",
                "tiktok", "spotify", "allevents", "setlist", "jambase")


def matches(name: str, domain: str) -> bool:
    label = domain.split(".")[0].replace("-", "")
    words = [w for w in loose_company_key(name).split() if len(w) > 2]
    flat = "".join(words)
    if not label or not flat:
        return False
    return label == flat or (len(label) >= 5 and (label in flat or flat in label)) or \
        (bool(words) and all(w in label for w in words))


def main(limit: int) -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    found = 0
    try:
        rows = ctx.db.q("""SELECT company_id, canonical_name, country FROM companies
                           WHERE company_type='concert_festival' AND status='QUALIFIED'
                             AND discovered_via='ticketmaster_promoter' AND website IS NULL
                           ORDER BY roster_estimate DESC LIMIT ?""", (limit,))
        for r in rows:
            url = ""
            for q in (f'"{r["canonical_name"]}" concerts', f'{r["canonical_name"]} promoter official site'):
                for res in websearch.search(ctx.http, q, limit=8):
                    dom = registrable_domain(res.url)
                    if not dom or any(b in dom for b in NOT_OFFICIAL):
                        continue
                    if matches(r["canonical_name"], dom):
                        url = f"https://{dom}"
                        break
                if url:
                    break
            if not url:
                continue
            dom = registrable_domain(url)
            if ctx.db.one("SELECT 1 FROM companies WHERE domain=? AND company_id!=?", (dom, r["company_id"])):
                continue
            ctx.db.x("UPDATE companies SET website=?, domain=?, last_researched=? WHERE company_id=?",
                     (url, dom, utcnow(), r["company_id"]))
            found += 1
            print(f"  {r['canonical_name'][:36]:36s} {url}", flush=True)
    finally:
        ctx.close()
    print(f"websites found: {found} of {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 2000))
