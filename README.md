# Music Industry Prospect Research Pipeline

An automation of `Music Industry Prospect Research SOP.pdf` (58 pages, 68 sections).
It discovers currently-active music companies, verifies them, finds decision-makers,
verifies their profiles, scores them, and emits the two final sheets the SOP requires.

The governing constraint is the SOP's own (§68): **quality over volume, and never guess.**
The system is built to refuse rather than fabricate — an unverifiable fact produces a
REVIEW row, never an invented value.

## Quick start

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env          # optional keys; the pipeline runs fully without them
PYTHONPATH=src .venv/bin/python -m mp.cli run --countries GB --limit 25
PYTHONPATH=src .venv/bin/python -m mp.cli export
```

Outputs land in `runs/<timestamp>/`:

| file | contents |
|---|---|
| `linkedin_final.csv` | §47 columns, exact order, one person per row |
| `instagram_final.csv` | §48 columns, exact order |
| `final_prospects.xlsx` | both sheets as formatted tabs |
| `companies_review.csv` | unresolved rows + what would resolve them + a `verdict` column you fill |
| `rejected.csv` | every rejection with its gate and reason |
| `run_report.md` | funnel, gate histogram, API budget, cache rates |

## Pipeline

| stage | SOP | what it does | sources |
|---|---|---|---|
| S1 discover | §3, §12–21 | activity-first: find current releases, work back to the company | MusicBrainz, iTunes |
| S2 resolve | §65.2 | raw label → canonical company + verified domain | MusicBrainz url-rels → Wikidata → Discogs → free search → Tavily |
| S3 verify | §6, §24 | roster, team, socials, country, type | website → Wikidata → MusicBrainz → iTunes |
| S4 gate | §29 | the eight qualification gates | pure logic |
| S5 people | §30–32 | decision-maker candidates | team page → search → *Apify last* |
| S6 profiles | §65.5 | personal LinkedIn / Instagram URLs | free search → *Apify* |
| S7 verify people | §34–36 | employment + identity verification | evidence counting, §35 checklist |
| S8 score | §39–46 | channel-specific scores + §54 rationales | deterministic + optional Groq |
| S9 export | §47–53 | dedupe, mandatory fields, sheets | — |

Run any stage alone (`mp verify`) or the whole chain (`mp run`). Every stage is
idempotent and re-runnable.

## The SOP lives in config, not code

`config/sop.yaml` is a direct transcription: the 33 approved countries (§7), the five
company types (§4), all ten hard-exclusion groups (§8), the eleven excluded brands (§9),
the seven role families (§30), the person exclusions (§31), and both sheets' exact
columns (§47/§48). `config/scoring.yaml` holds every point band from §40–§44.

**Changing research policy means editing YAML, not Python.** Tests assert the config
still matches the PDF, so drift is caught rather than discovered later.

## Cost control

Total cost of a full GB run today: **$0.00**.

- **Cache-first.** Every request is content-addressed to disk before any network egress.
  Re-running a run is nearly free.
- **Free sources do the heavy lifting.** MusicBrainz, Wikidata, iTunes, Deezer, Bandcamp
  and website scraping need no key and carry no quota.
- **Apify is structurally confined to people/profiles.** Five preconditions in
  `budget.yaml` are asserted in code; a call from any other stage raises
  `PreconditionFailed`. Budget cannot leak into discovery even by mistake.
- **Tavily is opportunistic.** It runs only on items free sources could not resolve and
  trips permanently on the first quota error. A run with `TAVILY_API_KEY` unset produces
  the same PASS set.
- **Groq classifies, never researches.** Enum-constrained outputs; generated prose is
  validated against collected facts and discarded if it introduces a number or entity
  that was not verified.

## Permanent memory

`data/master.sqlite` is the system of record (14 tables). Before any network call the
normalized company name is checked against `company_aliases`; known-rejected or
recently-researched companies are skipped. Human verdicts from `mp review-ingest` are
stored forever, so an adjudicated company never resurfaces.

## Optional keys

All optional — the pipeline degrades gracefully without each one.

| key | effect if absent |
|---|---|
| `SPOTIFY_CLIENT_ID/SECRET` | lose new-release→label discovery (§12) |
| `DISCOGS_TOKEN` | thinner catalogue/roster corroboration (§20) |
| `APIFY_TOKEN` | **people yield drops sharply**; profile resolution falls back to free search |
| `GROQ_API_KEY` | deterministic rationales; no review pre-adjudication |
| `TAVILY_API_KEY` | slightly fewer domains resolved |

## Commands

```
mp run|discover|resolve|verify|gate|people|profiles|verify-people|score|export
mp review-ingest runs/<ts>/companies_review.csv   # apply your verdicts (permanent)
mp sheets-sync <spreadsheet_id> [--dry-run]       # append to Google Sheets
mp status                                          # funnel + lifetime cost
mp config                                          # config hash + which keys are set
```

## Tests

```bash
.venv/bin/python -m pytest -q      # 218 tests, no network
```

The suites encode the SOP's own worked examples as executable expectations:
§56 → 98/97, §58 (23 artists) → still passes, §59 (Finance Director) → NOT FINAL,
§60 (no Instagram) → LinkedIn only, §61 marketing competitor → REJECT,
§62 studio → REJECT, §63 Sony Music Mexico → REJECT, §64 affiliate → do not auto-reject.
