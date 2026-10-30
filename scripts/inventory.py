"""Inventory of everything researched so far."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mp.db import Database  # noqa: E402

db = Database("data/master.sqlite")
n = lambda q: db.one(q)["n"]  # noqa: E731

Q = "status='QUALIFIED'"
print("=" * 60)
print("COMPANIES")
print("=" * 60)
print(f"  discovered (total)               {n('SELECT COUNT(*) n FROM companies')}")
for r in db.q("SELECT status, COUNT(*) n FROM companies GROUP BY status ORDER BY n DESC"):
    print(f"     {r['status']:14s} {r['n']}")
print()
print(f"  QUALIFIED (all 8 gates passed)   {n(f'SELECT COUNT(*) n FROM companies WHERE {Q}')}")
for label, col in (("website", "website"), ("LinkedIn page", "linkedin_url"),
                   ("Instagram", "instagram_url"), ("verified roster", "roster_estimate")):
    print(f"     with {label:22s} {n(f'SELECT COUNT(*) n FROM companies WHERE {Q} AND {col} IS NOT NULL')}")

print()
print("=" * 60)
print("PEOPLE")
print("=" * 60)
print(f"  people found (real)              {n('SELECT COUNT(*) n FROM people')}")
print(f"     employment verified           {n('SELECT COUNT(*) n FROM people WHERE current_verified=1')}")
print()
print(f"  CONTACTABLE (has profile URL)    {n('SELECT COUNT(DISTINCT person_id) n FROM profiles')}")
for ch in ("linkedin", "instagram"):
    tot = n(f"SELECT COUNT(*) n FROM profiles WHERE channel='{ch}'")
    ver = n(f"SELECT COUNT(*) n FROM profiles WHERE channel='{ch}' "
            f"AND verification_status='VERIFIED'")
    print(f"     {ch:10s} URLs {tot:4d}   verified {ver}")

print()
print(f"  IN FINAL SHEETS (90+)            "
      f"{n(chr(39).join(['SELECT COUNT(DISTINCT person_id) n FROM scores WHERE verdict=', 'FINAL PASS', '']))}")
for ch in ("linkedin", "instagram"):
    print(f"     {ch:10s} Final rows    "
          f"{n(f'SELECT COUNT(*) n FROM scores WHERE channel={chr(39)}{ch}{chr(39)} '
             f'AND verdict={chr(39)}FINAL PASS{chr(39)}')}")
print(f"  near-miss 85-89                  "
      f"{n(chr(39).join(['SELECT COUNT(*) n FROM scores WHERE verdict=', 'RESEARCH FURTHER', '']))}")

print()
print("=" * 60)
print("COMPANIES THAT PRODUCED PEOPLE")
print("=" * 60)
rows = db.q("""SELECT c.canonical_name AS co, c.country, c.roster_estimate AS ros,
                      COUNT(p.person_id) AS found,
                      SUM(CASE WHEN s.verdict='FINAL PASS' THEN 1 ELSE 0 END) AS passed
               FROM companies c
               JOIN people p ON p.company_id = c.company_id
               LEFT JOIN scores s ON s.person_id = p.person_id AND s.channel='linkedin'
               GROUP BY c.company_id ORDER BY passed DESC, found DESC""")
print(f"  {'COMPANY':32s} {'CC':3s} {'ROSTER':>6s} {'FOUND':>6s} {'PASS':>5s}")
for r in rows:
    print(f"  {r['co'][:30]:32s} {r['country'] or '??':3s} {str(r['ros'] or '?'):>6s} "
          f"{r['found']:>6d} {r['passed'] or 0:>5d}")
db.close()
