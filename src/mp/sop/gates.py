"""SS29 Company Qualification Gate -- the eight checks, plus the SS46 hard gates.

"Only companies passing the company gate should proceed to detailed person research."

That sentence is the entire cost model of this pipeline: gates run on free evidence,
and no paid call is permitted downstream of a failure. Each gate returns one of:

    PASS      continue
    FAIL      reject and persist the reason forever
    UNCLEAR   research further / verify further -- never a silent pass or reject

SS46 is enforced separately in scoring: "A numerical score cannot override a failed
hard gate."
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import config
from .exclusions import check_excluded_brand, check_hard_exclusion, is_approved_country

PASS, FAIL, UNCLEAR = "PASS", "FAIL", "UNCLEAR"


@dataclass(slots=True)
class Gate:
    id: str
    name: str
    result: str
    reason: str = ""
    action: str = ""            # continue | reject | research_further | deprioritize


@dataclass(slots=True)
class GateReport:
    gates: list[Gate] = field(default_factory=list)

    @property
    def failed(self) -> list[Gate]:
        return [g for g in self.gates if g.result == FAIL]

    @property
    def unclear(self) -> list[Gate]:
        return [g for g in self.gates if g.result == UNCLEAR]

    @property
    def passed_all(self) -> bool:
        return all(g.result == PASS for g in self.gates)

    @property
    def verdict(self) -> str:
        """REJECT | REVIEW | PASS -- the company-level routing decision."""
        if self.failed:
            return "REJECT"
        if self.unclear:
            return "REVIEW"
        return "PASS"

    @property
    def summary(self) -> str:
        return "; ".join(f"{g.id}:{g.result}" for g in self.gates)

    def as_dict(self) -> dict:
        return {g.id: {"name": g.name, "result": g.result, "reason": g.reason,
                       "action": g.action} for g in self.gates}

    def first_failure(self) -> Gate | None:
        return self.failed[0] if self.failed else None


def evaluate(facts: dict) -> GateReport:
    """Run SS29 Gates 1-8 over a verified company's structured facts.

    Expected keys (all optional; absence is handled as UNCLEAR, never as a pass):
        name, description, website_text, country, country_confidence,
        company_type, type_confidence, parent_org, roster_estimate,
        roster_resolved, artist_count, activity_signal_count,
        recent_activity_count, evidence_urls, strong_evidence_count
    """
    report = GateReport()
    sop = config.sop()

    # -- Gate 1: Approved geography (SS7) ---------------------------------
    country = (facts.get("country") or "").upper()
    if not country:
        report.gates.append(Gate(
            "G1", "Geography", UNCLEAR,
            "Country could not be determined. SS7 forbids inferring geography from "
            "assumptions -- verify via website address, Wikidata or MusicBrainz.",
            "research_further"))
    elif is_approved_country(country):
        report.gates.append(Gate("G1", "Geography", PASS,
                                 f"{country} is an approved country.", "continue"))
    else:
        report.gates.append(Gate("G1", "Geography", FAIL,
                                 f"{country} is not in the SS7 approved country list.",
                                 "reject"))

    # -- Gate 2: Company type (SS4) ---------------------------------------
    ctype = facts.get("company_type") or ""
    types = sop["company_types"]
    if not ctype:
        report.gates.append(Gate("G2", "Company Type", UNCLEAR,
                                 "Company category not established.", "research_further"))
    elif ctype in types:
        conditional = types[ctype].get("conditional", False)
        if conditional:
            # SS4.5: concert/festival businesses are not a default target type.
            report.gates.append(Gate(
                "G2", "Company Type", UNCLEAR,
                f"'{types[ctype]['label']}' is a conditional category (SS4.5): only "
                "qualifies at a professional music-industry level with recognized "
                "artists. Confirm before proceeding.", "research_further"))
        else:
            report.gates.append(Gate("G2", "Company Type", PASS,
                                     f"Approved category: {types[ctype]['label']}.",
                                     "continue"))
    else:
        report.gates.append(Gate("G2", "Company Type", FAIL,
                                 f"'{ctype}' is not an approved SS4 category.", "reject"))

    # -- Gate 3: Hard exclusions (SS8) ------------------------------------
    # NOTE: deliberately name+description only, matching every other call site and
    # the whole test suite. The raw scraped website_text (6000 chars of nav links,
    # footer, cookie banner, embedded Spotify/Bandcamp players) was being matched
    # against the SS8 keyword list here, so a genuine label with a "Blog" nav item
    # or a Spotify follow-link in its footer was hard-excluded as a "media company"
    # or "streaming service" -- caught via a spot-check of G3 rejections that found
    # entries like Heavenly-Recordings-shaped labels rejected for "matched 'blog'"
    # or "matched 'Spotify'". description is the clean <meta name="description">
    # tag; website_text is unfiltered page text and is far too noisy for a keyword
    # phrase match against brand names and generic business-type words.
    hard = check_hard_exclusion(facts.get("name", ""), facts.get("description", ""))
    if hard.excluded:
        report.gates.append(Gate("G3", "Hard Exclusion", FAIL,
                                 f"{hard.reason} (matched '{hard.matched}')", "reject"))
    else:
        report.gates.append(Gate("G3", "Hard Exclusion", PASS,
                                 "No SS8 excluded category matched.", "continue"))

    # -- Gate 4: Existing relationship exclusion (SS9) --------------------
    brand = check_excluded_brand(
        facts.get("name", ""), facts.get("parent_org", "") or "",
        domain=facts.get("domain", "") or "",
        instagram=facts.get("instagram_url", "") or "",
        linkedin=facts.get("linkedin_url", "") or "")
    if brand.outcome == "EXCLUDED":
        report.gates.append(Gate("G4", "Existing Relationship", FAIL, brand.reason, "reject"))
    elif brand.outcome == "REVIEW":
        report.gates.append(Gate("G4", "Existing Relationship", UNCLEAR, brand.reason,
                                 "research_further"))
    else:
        report.gates.append(Gate("G4", "Existing Relationship", PASS,
                                 brand.reason or "No excluded brand relationship.",
                                 "continue"))

    # -- Gate 5: Multi-artist relevance (SS29) ----------------------------
    artists = int(facts.get("artist_count") or 0)
    # A verified roster is itself multi-artist evidence -- a 14-artist roster page
    # proves the point even when discovery only happened to catch one release.
    if facts.get("roster_resolved") and int(facts.get("roster_estimate") or 0) >= 2:
        artists = max(artists, int(facts["roster_estimate"]))
    if artists >= 2:
        report.gates.append(Gate("G5", "Multi-Artist Relevance", PASS,
                                 f"{artists} distinct artists evidenced.", "continue"))
    elif artists == 1:
        report.gates.append(Gate(
            "G5", "Multi-Artist Relevance", UNCLEAR,
            "Only one artist evidenced. SS4.2 warns against confusing a single-artist "
            "manager with a genuine multi-artist company.", "research_further"))
    else:
        report.gates.append(Gate("G5", "Multi-Artist Relevance", UNCLEAR,
                                 "No artist relationships evidenced yet.",
                                 "research_further"))

    # -- Gate 6: Roster (SS5) ---------------------------------------------
    band = sop["roster"]["strongest_range"]
    if not facts.get("roster_resolved"):
        report.gates.append(Gate("G6", "Roster", UNCLEAR,
                                 "Roster could not be verified (SS6: never guess).",
                                 "research_further"))
    else:
        n = int(facts.get("roster_estimate") or 0)
        if band["min"] <= n <= band["max"]:
            report.gates.append(Gate("G6", "Roster", PASS,
                                     f"{n} artists is within the strongest range "
                                     f"({band['min']}-{band['max']}).", "continue"))
        elif n < band["min"]:
            # Checked BEFORE the "slightly above" branch -- otherwise a 1-artist
            # roster would pass as if it merely exceeded the preferred range.
            report.gates.append(Gate("G6", "Roster", UNCLEAR,
                                     f"Only {n} artists -- below the multi-artist "
                                     "threshold. Verify further.", "research_further"))
        elif n <= 30:
            # SS5: "Do not reject a strong company simply because the estimate is
            # slightly above 20."
            report.gates.append(Gate("G6", "Roster", PASS,
                                     f"{n} artists is above the preferred range but "
                                     "still focused (SS5 permits this).", "continue"))
        else:
            report.gates.append(Gate(
                "G6", "Roster", UNCLEAR,
                f"{n} artists is a very large roster. SS5: normally lower priority "
                "unless a clearly relevant specialist division exists.", "research_further"))

    # -- Gate 7: Current activity (SS27) ----------------------------------
    recent = int(facts.get("recent_activity_count") or 0)
    total = int(facts.get("activity_signal_count") or 0)
    if recent >= 1:
        report.gates.append(Gate("G7", "Current Activity", PASS,
                                 f"{recent} recent activity signal(s).", "continue"))
    elif total >= 1:
        report.gates.append(Gate("G7", "Current Activity", UNCLEAR,
                                 f"{total} activity signal(s), none recent. SS27 "
                                 "requires current relevance.", "deprioritize"))
    else:
        report.gates.append(Gate("G7", "Current Activity", UNCLEAR,
                                 "No current activity evidenced.", "deprioritize"))

    # -- Gate 8: Evidence (SS38) ------------------------------------------
    strong = int(facts.get("strong_evidence_count") or 0)
    urls = int(facts.get("evidence_urls") or 0)
    if strong >= 2:
        report.gates.append(Gate("G8", "Evidence", PASS,
                                 f"{strong} strong-tier evidence URLs.", "continue"))
    elif urls >= 1:
        report.gates.append(Gate("G8", "Evidence", UNCLEAR,
                                 f"Only {urls} evidence URL(s) and {strong} strong-tier. "
                                 "SS38: a final record must not rest on weak evidence.",
                                 "research_further"))
    else:
        report.gates.append(Gate("G8", "Evidence", UNCLEAR,
                                 "No supporting evidence URLs recorded.",
                                 "research_further"))

    return report
