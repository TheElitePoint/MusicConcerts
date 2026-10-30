"""Append only new FINAL PASS members of the signed-in Instagram batch."""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mp import config  # noqa: E402
from mp.pipeline import Context  # noqa: E402
from mp.stages.s8_score import build_facts  # noqa: E402
from mp.stages.s9_export import _mandatory_ok, _row_for  # noqa: E402

SPREADSHEET_ID = "1OihYVSE8SGM2nKiBqyXK4Aps1jz6NrKcfTpdrgmyRJc"
CREDENTIALS = "/Users/anirudhtyagi/Downloads/rosy-spring-507811-c2-79a06cc1bdba.json"
CHANGED = ROOT / "data/instagram_runs/browser_verified_person_ids_20260908.json"
AUDIT = ROOT / "data/google_sheet_sync_instagram_browser_batch_20260908.json"

PROFILE_DETAILS = {
    25:  {"name": "Mari* 木村真理", "posts": 2589, "followers": 5864, "following": 2717, "bio": "From Japan, based in London. Music selector, Radio & Podcast producer/presenter, @gondwanarecords.", "website": "https://linktr.ee/marinx611"},
    29:  {"name": "Emma Snook", "posts": 42, "followers": 630, "following": 1367, "bio": "@partisanrecords"},
    30:  {"name": "annie horvath", "posts": 507, "followers": 968, "following": 1687, "bio": "Personal account."},
    33:  {"name": "Sara Dempsey", "posts": 1629, "followers": 1643, "following": 4629, "bio": "music among other things"},
    38:  {"name": "Alexandra Rosen", "posts": 225, "followers": 1295, "following": 2120, "bio": "brooklyn, ny | @partisanrecords | @alexdoespokes"},
    54:  {"name": "James Nicholls", "posts": 387, "followers": 952, "following": 831, "bio": "Humble servant to Music & Film. Creative Director: @fire_records; @weird_walk; @earthrecordings.", "website": "https://linktr.ee/sirspires"},
    56:  {"name": "Gros Alice", "posts": 9, "followers": 486, "following": 705},
    59:  {"name": "Jonny Halliday", "posts": 80, "followers": 1062, "following": 1352, "bio": "Paul McCartney advocate"},
    92:  {"name": "Dan Hardingham", "posts": 56, "followers": 269, "following": 561},
    133: {"name": "Dominique Zgarka", "posts": 361, "followers": 596, "following": 1906, "bio": "Entrepreneur", "website": "https://thesrg-ilsgroup.com"},
    135: {"name": "Ben Howe", "posts": 148, "followers": 549, "following": 52, "website": "https://www.archhill.co.nz"},
    136: {"name": "Hariet Ellis", "posts": 188, "followers": 908, "following": 1097, "bio": "@flyingnun | @dick_move_dick_pics | @nanoise"},
    174: {"name": "Jake Williams", "posts": 104, "followers": 402, "following": 1100, "bio": "Record labelman @farout_recordings | Unfortunately I also DJ sometimes", "website": "https://soundcloud.com/balamii/far-out-recordings-jan-2020"},
    180: {"name": "Zak Benjamin", "posts": 42, "followers": 740, "following": 944},
    185: {"name": "W I L L S T R E E T", "posts": 1776, "followers": 1419, "following": 4039, "bio": "Founder / A&R at Independent Record Label @chessclubrecords. Cat Dad."},
    191: {"name": "Sarah Kesselman", "posts": 280, "followers": 1050, "following": 1583, "bio": "Came for the snacks, stayed for the puns."},
    199: {"name": "reagan hunley", "posts": 121, "followers": 1263, "following": 974, "bio": "jmu alum; ephesians 2:8", "website": "https://vsco.co/reagan-hunley"},
    200: {"name": "Michael Goldwasser", "posts": 491, "followers": 2453, "following": 2142, "bio": "Producer: Ziggy Stardub, Dub Side of the Moon, Radiodread, Lonely Hearts Dub, Thrillah, Goldswagger, Jason Mraz."},
    206: {"name": "Kathie Reed Merritt", "posts": 168, "followers": 550, "following": 871},
    208: {"name": "Sue Lucarelli", "posts": 1457, "followers": 2155, "following": 2689, "bio": "President @epitaphrecords; Cooooooooool mom to @gunnarsharron", "website": "https://www.epitaph.com/"},
    212: {"name": "Felicia Risolo", "posts": 56, "followers": 1171, "following": 1901, "bio": "Marketing @epitaphrecords; Los Angeles via Long Island"},
    211: {"name": "Alex Bartholemy", "posts": 484, "followers": 206, "following": 563, "bio": "Director of digital operations @epitaphrecords + @antirecords | music, gadgets, video games, sports, dogs | Seattle, WA", "website": "https://winstonlegthigh.xyz"},
    379: {"name": "Mike Ajayi", "posts": 69, "followers": 3486, "following": 3124, "bio": "President, @amf_records x @blackbutterrecords x @betterwithfriendsco"},
    411: {"name": "e v e", "posts": 45, "followers": 2237, "following": 4107, "bio": "head of a&r @amf_records @blackbutterrecords", "website": "https://open.spotify.com/playlist/37i9dQZF1DX0b56pNrpTUT"},
    913: {"name": "Jedediah Caractacus Morgans", "posts": 295, "followers": 1344, "following": 1848, "bio": "@opuskink | @sorecordings | @hideousminkrecords - @hideous.mag", "website": "https://linktr.ee/HideousMinkRecords"},
    2337:{"name": "marisa kurtz", "posts": 601, "followers": 1126, "following": 1375, "bio": "Accidentally high maintenance", "website": "https://www.fearlessrecords.com"},
    2338:{"name": "Jacob Round", "posts": 268, "followers": 3170, "following": 4583, "bio": "Founder @PureNoiseRecs. Lover of: Raiders. LA Kings. Warriors. Golf. Cats.", "website": "https://purenoise.net"},
    2344:{"name": "Greg Hudik", "posts": 1, "followers": 61, "following": 1, "bio": "CEO Platinum Records Nashville", "website": "https://www.platinumrecordsnashville.com"},
    2335:{"name": "Claire Thompson", "posts": 68, "followers": 402, "following": 1039, "bio": "@fearlessrecords // CLU '20"},
    2412:{"name": "Becky", "posts": 7, "followers": 3508, "following": 1284, "bio": "digital marketing at @capitolmusic"},
    2417:{"name": "Wez Saunders", "posts": 113, "followers": 20300, "following": 3635, "bio": "Chairman/CEO/co-owner @defectedrecords @glitterboxibiza | @westham 'till I die", "website": "https://wezsaunders.substack.com"},
    2594:{"name": "Joris Liebregt", "posts": 71, "followers": 268, "following": 505, "bio": "Werk: @doxamsterdam // @newcoolcollectivehq foto", "website": "https://www.jorisliebregt.nl"},
    2597:{"name": "Tony Vandenbogaerde", "posts": 9, "followers": 344, "following": 429, "bio": "my passion and mission @maywayrecords"},
    214: {"name": "Matt Bury", "posts": 295, "followers": 330, "following": 449},
    349: {"name": "Alex Knezevic", "posts": 9, "followers": 125, "following": 622},
    360: {"name": "Lisa Garelick", "posts": 264, "followers": 1156, "following": 1788, "bio": "Dax Shepard (the dog); @epitaphrecords"},
    385: {"name": "Phil Waldorf", "posts": 1107, "followers": 1844, "following": 1058, "website": "https://secretlygroup.com"},
    388: {"name": "Rachel", "posts": 18, "followers": 1264, "following": 1511, "bio": "⚽️ + 🎥 + 💽 @secretly @secretlycanadian @jagjaguwar @deadoceans"},
    394: {"name": "Kayla Ganz", "posts": 735, "followers": 490, "following": 633},
    415: {"name": "Jeff Stevenson", "posts": 96, "followers": 539, "following": 668},
    409: {"name": "therealwillswan", "posts": 799, "followers": 65300, "following": 498, "bio": "Guitar player for Dance Gavin Dance, Royal Coda, and Secret Band. Owner of Blue Swan Records.", "website": "https://youtu.be/HzVPPbaHk7s"},
    410: {"name": "Sergio E. Medina", "posts": 21, "followers": 27300, "following": 1898, "bio": "VP @blueswanrecords; Draw @exitsmile; Guitar @eidola + @royal.coda; Bass @dancegavindance."},
    426: {"name": "Mitch Daniels", "posts": 25, "followers": 284, "following": 393, "bio": "I'm not a doctor, I'm just an MD."},
    427: {"name": "Jeanette Wall", "posts": 1398, "followers": 3208, "following": 5828, "bio": "women have an ocean of secrets in their mind"},
    443: {"name": "Jenna Barber", "posts": 0, "followers": 571, "following": 449, "bio": "@epitaphrecords @antirecords @hellcatrecords"},
    457: {"name": "Natalie Turano", "posts": 236, "followers": 650, "following": 749, "bio": "My life in pictures."},
    494: {"name": "Jonathan Tristan Tuite", "posts": 639, "followers": 697, "following": 646, "bio": "Flenser guy. Mostly cats."},
    498: {"name": "Quentin Martins", "posts": 23, "followers": 441, "following": 947, "bio": "Hello!"},
    509: {"name": "ilana lombardo", "posts": 633, "followers": 1503, "following": 1594, "bio": "charmed, i'm sure"},
    545: {"name": "Maartje Albers", "posts": 60, "followers": 1755, "following": 1578, "bio": "Amsterdam; Artist Manager & TV/Perspromotor @universalmusicnl @8ballmusic @dinomusic_nl"},
    572: {"name": "Pedro Vian", "posts": 105, "followers": 6800, "following": 1312, "bio": "Music Composer; @modernobscuremusic; @mom.publishing", "website": "https://linktr.ee/pedro_vian"},
    581: {"name": "Pedro Vindeirinho", "posts": 1448, "followers": 1253, "following": 264, "bio": "@rastilho_records @moonspellofficial @rastilho_distribution @almamaterbooksandrecords | LEIRIA, PT", "website": "http://www.rastilho.com/"},
    587: {"name": "Bartek Kopiniak", "posts": 758, "followers": 1077, "following": 1645, "bio": "Owner x artist manager @fala.label; @maks_tachasiuk @brielaa @rembikowski_", "website": "https://www.falalabel.pl/"},
    1009:{"name": "Rishi Saluja", "posts": 95, "followers": 542, "following": 685, "bio": "@cryer.mya"},
    1010:{"name": "Hayley Absalom", "posts": 478, "followers": 487, "following": 1715, "bio": "@transgressivehq"},
    1017:{"name": "Carole Seager-Dupuy", "posts": 270, "followers": 310, "following": 926, "bio": "London // International at @transgressivehq"},
    1018:{"name": "Jack Hedges", "posts": 348, "followers": 1014, "following": 1874, "bio": "Co-founder @canvasbackmusic and GM @transgressivehq North America"},
    2820:{"name": "Jonathan Rego", "posts": 299, "followers": 2822, "following": 3992, "bio": "music brings us all together."},
    2836:{"name": "Frank Vredeveldt", "posts": 21, "followers": 585, "following": 1249, "bio": "Head of A&R Soave Records"},
    2839:{"name": "Luca Mello Rella", "posts": 645, "followers": 1140, "following": 2968, "bio": "Label Manager @soave.records"},
    1090:{"name": "tom abbs", "posts": 32, "followers": 463, "following": 563, "bio": "", "website": "http://northernspyrecords.com/"},
    489: {"name": "Alberto Farinas", "posts": 924, "followers": 1174, "following": 1378, "bio": "Create with intent", "website": "http://animalmusicstudios.com/"},
    2333:{"name": "Jen Danielson", "posts": 90, "followers": 992, "following": 1388, "bio": "@bigloud | Nashville, TN"},
    673: {"name": "Jay Reason", "posts": 55, "followers": 4705, "following": 1286, "bio": "Independent Music Professional | @staticerarecords | @wargodcollective | Diablo's Den Podcast", "website": "https://linktr.ee/jayreason"},
    683: {"name": "Severin Most", "posts": 0, "followers": 1390, "following": 3650},
    703: {"name": "Bertie Blackman", "posts": 603, "followers": 4761, "following": 1756, "website": "https://www.bertieblackman.net"},
    1066:{"name": "Kevin Duneman", "posts": 65, "followers": 799, "following": 698, "bio": "paintings @kevin.duneman; music @pretty_good_cloud_day_", "website": "https://linktr.ee/goodcloudday"},
    1085:{"name": "Hannah Houser", "posts": 2828, "followers": 2944, "following": 2808, "bio": "Artist management; label mgmt: @northernspyrecs", "website": "https://www.hannahhouser.com"},
    1397:{"name": "Ryley Remedios", "posts": 246, "followers": 1226, "following": 1092, "bio": "baked in a buttery crispy flake"},
    1420:{"name": "Greg Thompson", "posts": 833, "followers": 5093, "following": 2171},
    1994:{"name": "Mauricio Bussab", "posts": 275, "followers": 1991, "following": 1599, "bio": "Fundador da Tratore distribuidora digital de musica. Founder of Tratore Distribution.", "website": "https://sdz.sh/GY1tQD"},
    2474:{"name": "Lexie Viklund", "posts": 138, "followers": 2207, "following": 1505, "bio": "NYC. A&R @betternoisemusic; @pigmilknyc", "website": "https://linktr.ee/pigmilknyc"},
    2818:{"name": "Markus Riedler", "posts": 222, "followers": 1672, "following": 599, "bio": "Captain Napalm's best Buddy. Working for Rock/Metal company is not a job, it is a lifestyle. #napalmrecordsofficial", "website": "https://www.napalmrecords.com"},
    2823:{"name": "Bas van Leeuwen", "posts": 189, "followers": 1189, "following": 1305, "bio": "Founder @chillhopmusic | Rotterdam, NL"},
}


def key(value: object) -> str:
    return str(value or "").strip().lower().rstrip("/")


def row_numbers(updated_range: str) -> tuple[int, int]:
    match = re.search(r"![A-Z]+(\d+):[A-Z]+(\d+)$", updated_range)
    if not match:
        raise RuntimeError(f"unexpected updated range: {updated_range}")
    return int(match.group(1)), int(match.group(2))


def main() -> int:
    ids = sorted({int(x) for x in json.loads(CHANGED.read_text(encoding="utf-8"))} | {38, 136, 174, 180, 208, 212, 349, 360, 388, 489, 545, 572, 581, 587, 1009, 1010, 1017, 1018, 1090, 1994, 2333, 2338, 2417, 2594, 2597, 2820, 2836, 2839})
    sop = config.sop()
    ctx = Context.create(db_path="data/master.sqlite")
    candidate_rows: list[tuple[int, int, dict]] = []
    try:
        for person_id in ids:
            score_row = ctx.db.one(
                "SELECT * FROM scores WHERE person_id=? AND channel='instagram'",
                (person_id,),
            )
            if not score_row or score_row["verdict"] != "FINAL PASS":
                continue
            facts = build_facts(ctx, person_id)
            row = _row_for(facts, score_row, "instagram", sop)
            ok, missing = _mandatory_ok(row, sop["sheets"]["instagram"]["mandatory"], sop["sheets"]["not_found_literal"])
            if not ok:
                raise RuntimeError(f"person {person_id} missing mandatory fields: {missing}")
            candidate_rows.append((person_id, int(facts["company_id"]), row))
    finally:
        ctx.close()

    creds = Credentials.from_service_account_file(CREDENTIALS, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    api = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
    meta = api.get(spreadsheetId=SPREADSHEET_ID, fields="properties.title,sheets.properties").execute()
    props = next(s["properties"] for s in meta["sheets"] if s["properties"]["title"] == "Instagram")
    live = api.values().get(spreadsheetId=SPREADSHEET_ID, range="'Instagram'!A1:AC2000").execute().get("values", [])
    if not live:
        raise RuntimeError("Instagram tab is empty")
    header, *values = live
    existing = [dict(zip(header, row)) for row in values]
    existing_urls = {key(row.get("Person Instagram URL")) for row in existing}
    existing_people = {(key(row.get("Company Name")), key(row.get("Person Name"))) for row in existing}
    company_counts = Counter(key(row.get("Company Name")) for row in existing)

    append_rows: list[list] = []
    appended_ids: list[int] = []
    skipped: list[dict] = []
    for person_id, _company_id, row in candidate_rows:
        company_key = key(row["Company Name"])
        person_key = (company_key, key(row["Person Name"]))
        url_key = key(row["Person Instagram URL"])
        if url_key in existing_urls or person_key in existing_people:
            skipped.append({"person_id": person_id, "reason": "already present"})
            continue
        if company_counts[company_key] >= 10:
            skipped.append({"person_id": person_id, "reason": "company cap"})
            continue
        detail = PROFILE_DETAILS[person_id]
        enriched = dict(row)
        enriched.update({
            "Scrape Status": "SOP FINAL",
            "IG Full Name": detail.get("name", ""),
            "IG Bio": detail.get("bio", ""),
            "IG Followers": detail.get("followers", ""),
            "IG Following": detail.get("following", ""),
            "IG Posts": detail.get("posts", ""),
            "IG Website": detail.get("website", ""),
            "IG Email (from bio)": "",
            "IG Profile Pic": "",
            "Last Scraped": "2026-09-08",
        })
        append_rows.append([enriched.get(column, "") for column in header])
        appended_ids.append(person_id)
        existing_urls.add(url_key)
        existing_people.add(person_key)
        company_counts[company_key] += 1

    result = {
        "spreadsheet_title": meta["properties"]["title"],
        "final_candidates": len(candidate_rows),
        "rows_before": len(values),
        "appended": 0,
        "skipped": skipped,
    }
    if append_rows:
        response = api.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="'Instagram'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": append_rows},
        ).execute()
        updated_range = response["updates"]["updatedRange"]
        first_row, last_row = row_numbers(updated_range)
        api.batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [
                {"copyPaste": {
                    "source": {"sheetId": props["sheetId"], "startRowIndex": first_row - 2, "endRowIndex": first_row - 1, "startColumnIndex": 0, "endColumnIndex": len(header)},
                    "destination": {"sheetId": props["sheetId"], "startRowIndex": first_row - 1, "endRowIndex": last_row, "startColumnIndex": 0, "endColumnIndex": len(header)},
                    "pasteType": "PASTE_FORMAT", "pasteOrientation": "NORMAL",
                }},
                {"repeatCell": {
                    "range": {"sheetId": props["sheetId"], "startRowIndex": first_row - 1, "endRowIndex": last_row, "startColumnIndex": 0, "endColumnIndex": len(header)},
                    "cell": {"userEnteredFormat": {"verticalAlignment": "TOP", "wrapStrategy": "WRAP"}},
                    "fields": "userEnteredFormat.verticalAlignment,userEnteredFormat.wrapStrategy",
                }},
            ]},
        ).execute()
        con = __import__("sqlite3").connect(ROOT / "data/master.sqlite", timeout=60)
        try:
            for person_id in appended_ids:
                con.execute(
                    "INSERT OR IGNORE INTO sheet_membership(person_id,channel,first_seen_at,batch) VALUES (?, 'instagram', datetime('now'), ?)",
                    (person_id, "instagram_browser_20260908"),
                )
            con.commit()
        finally:
            con.close()
        result.update({"appended": len(append_rows), "updated_range": updated_range, "appended_ids": appended_ids})

    AUDIT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
