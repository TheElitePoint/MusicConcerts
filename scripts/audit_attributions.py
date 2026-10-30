"""Audit final-sheet rows for the two attribution errors that survive verification.

Employment verification confirms that a person's LinkedIn profile exists and that
sources agree -- it does NOT confirm the person works at the company WE assigned
them to. Two failure modes slip through:

  A) The headline names a different employer ("Owner @ Western Publicity" filed
     under Bloodshot Records). SS42 C3 forbids a FINAL PASS on materially uncertain
     employment, and this is not uncertain, it is contradicted.

  B) A company name parsed as a person ("Chess Club" at Chess Club Records),
     which SS50 rules out: one real person per row.

Both are deliberately narrow. Only "@ X" and "at X" are read as employer markers --
"of X" and "for X" introduce a department far more often than a company ("Head of
Marketing"), and treating those as employers produced 45 false positives against 3
real ones when measured. A conflict is only recorded when NO candidate employer in
the headline matches the assigned company, so "Managing Director at Brownswood
Music Ltd" under Brownswood Recordings correctly stays.

Run with --apply to write rejections; the default is a dry run.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mp.db import utcnow                                   # noqa: E402
from mp.normalize import normalize_company_name            # noqa: E402
from mp.pipeline import Context                            # noqa: E402

EMPLOYER_MARKER = re.compile(
    r"(?:\s@\s*|\bat\s+)([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})")


# Words that carry no identity: every second label is "... Records". Comparing
# full strings makes "Earache" and "Earache Records" look like different employers,
# which would reject the founder of the very company he is filed under.
GENERIC = {"records", "record", "recordings", "recording", "music", "musik",
           "group", "ltd", "limited", "inc", "llc", "gmbh", "bv", "ab", "as",
           "co", "company", "the", "mgmt", "management", "entertainment",
           "productions", "production", "label", "media", "sarl", "sa", "oy"}


def core_name(value: str) -> str:
    """The distinctive part of a company name, generic industry words removed."""
    tokens = [t for t in normalize_company_name(value).split() if t not in GENERIC]
    return " ".join(tokens) or normalize_company_name(value)


def employer_matches(expected: str, found: str) -> bool:
    a, b = core_name(expected), core_name(found)
    if not a or not b:
        return False
    if a == b:
        return True
    # Containment only on a distinctive core, so "Atlantic" never absorbs "Earache"
    # while "Embassy" still matches "Embassy of Music".
    return (len(a) >= 4 and a in b) or (len(b) >= 4 and b in a)


def conflicts(title: str, company: str) -> list[str]:
    """Employers named in the headline that contradict the assigned company."""
    cands = [m.group(1).strip() for m in EMPLOYER_MARKER.finditer(title or "")]
    cands = [c for c in cands if len(normalize_company_name(c)) >= 4]
    if not cands or any(employer_matches(company, c) for c in cands):
        return []
    return cands


# Words that appear in brand strings but never in a personal name. Needed to tell
# "Fabtone Fashion" (a brand) from "Dunya Cengiz" (a founder who named the label
# after herself) -- both share every other structural property.
BRAND_WORDS = {"fashion", "premiering", "official", "store", "shop", "studio",
               "studios", "sounds", "sound", "worldwide", "online", "tv", "radio",
               "live", "agency", "digital", "collective", "distribution", "booking",
               "publishing", "audio", "artists", "records", "team", "info", "news"}


def name_is_company(full_name: str, company: str) -> bool:
    """Whether the 'person' is really the company (SS50 parsing failure).

    The record is the company, not a person, when stripping the company's own
    tokens and non-personal vocabulary leaves nothing that could be a name.
    A founder who shares a name with their label ("Shigeru Urashima" at Urashima,
    "Dunya Cengiz" at Dunya) keeps a real surname after that strip and survives;
    "Fabtone Fashion" and "RK music premiering" do not.
    """
    if not employer_matches(company, full_name):
        return False
    company_tokens = set(normalize_company_name(company).split())
    residue = [t for t in normalize_company_name(full_name).split()
               if t not in company_tokens and t not in GENERIC
               and t not in BRAND_WORDS]
    return not residue


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    ctx = Context.create(db_path="data/master.sqlite")
    try:
        rows = ctx.db.q("""SELECT DISTINCT p.person_id, p.full_name, p.title,
                                  c.canonical_name
                           FROM scores s
                           JOIN people p ON p.person_id = s.person_id
                           JOIN companies c ON c.company_id = p.company_id
                           WHERE p.status != 'REJECTED'
                             AND c.status = 'QUALIFIED'""")
        print(f"auditing {len(rows)} sheet-eligible people\n")

        bad: list[tuple[int, str, str]] = []
        for r in rows:
            found = conflicts(r["title"] or "", r["canonical_name"])
            if found:
                bad.append((r["person_id"],
                            f"headline names {found[0]!r}, not {r['canonical_name']!r}",
                            f"{r['full_name']} | {r['title']}"))
            elif name_is_company(r["full_name"], r["canonical_name"]):
                bad.append((r["person_id"],
                            f"'{r['full_name']}' is the company, not a person (SS50)",
                            f"{r['full_name']} @ {r['canonical_name']}"))

        for pid, reason, detail in bad:
            print(f"  [{pid:5d}] {detail[:60]:62s} {reason}")
        print(f"\n{len(bad)} attribution errors found")

        if args.apply and bad:
            for pid, reason, _ in bad:
                ctx.db.x("""UPDATE people SET status='REJECTED', reject_reason=?,
                            last_researched=? WHERE person_id=?""",
                         (reason, utcnow(), pid))
            print(f"rejected {len(bad)} people")
        elif bad:
            print("dry run -- pass --apply to write")
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
