"""Write the current SOP-eligible Instagram verification queue for browser research."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp.pipeline import Context  # noqa: E402

OUTPUT = ROOT / "data" / "instagram_runs" / "dual_100_queue_20260902.json"


def main() -> int:
    ctx = Context.create(db_path="data/master.sqlite")
    try:
        rows = ctx.db.q(
            """SELECT p.person_id,p.full_name,p.title,c.company_id,
                      c.canonical_name AS company,c.instagram_url AS company_instagram_url,
                      li.url AS linkedin_url,COALESCE(s.total,0) AS linkedin_score
                 FROM people p
                 JOIN companies c USING(company_id)
                 JOIN profiles li ON li.person_id=p.person_id AND li.channel='linkedin'
                 LEFT JOIN scores s ON s.person_id=p.person_id AND s.channel='linkedin'
                WHERE p.current_verified=1 AND p.status!='REJECTED'
                  AND c.status='QUALIFIED'
                  AND li.verification_status='VERIFIED'
                  AND c.instagram_url IS NOT NULL AND c.instagram_url!=''
                  AND NOT EXISTS (
                      SELECT 1 FROM profiles ig
                       WHERE ig.person_id=p.person_id AND ig.channel='instagram'
                         AND ig.verification_status='VERIFIED')
                ORDER BY COALESCE(s.total,0) DESC,c.canonical_name,p.full_name"""
        )
        payload = [dict(row) for row in rows]
    finally:
        ctx.close()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"rows": len(payload), "companies": len({r['company_id'] for r in payload}),
                      "output": str(OUTPUT)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
