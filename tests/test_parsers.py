"""Unit tests for the pure parsing helpers of the scrapers (no network, no Google Sheets).
Run: pip install -r requirements.txt pytest && python -m pytest -q"""
from datetime import date

from scraper import probate, tax


# ---------------------------------------------------------------- probate: decedent names
def test_index_order_estate():
    assert probate.name_from_index(["NYSTROM RACHEL RENE ESTATE"]) == ("NYSTROM", "RACHEL", "RENE")


def test_natural_order_estate_of():
    assert probate.name_from_index(["ESTATE OF ARACELY G URDA DECEASED"]) == ("URDA", "ARACELY", "G")


def test_no_estate_marker_is_not_a_decedent():
    assert probate.name_from_index(["JOHN SMITH"]) is None


def test_two_different_estates_are_ambiguous():
    assert probate.name_from_index(["NYSTROM RACHEL ESTATE", "JUAREZ PETE DECD"]) is None


def test_decedent_falls_back_to_grantee():
    d = {"grantor": ["ACME BANK"], "grantee": ["JUAREZ PETE DECD AKA"]}
    assert probate.decedent(d) == ("JUAREZ", "PETE", "")


def test_decedent_from_other_parties():
    d = {"grantor": [], "grantee": [], "parties": [{"name": "ESTATE OF MARY ANN LEE"}]}
    assert probate.decedent(d) == ("LEE", "MARY", "ANN")


def test_court_style_names():
    assert probate.name_from_style("In the Estate Of Patricia B. Assenheimer") == ("ASSENHEIMER", "PATRICIA", "B")
    assert probate.name_from_style("The Estate of Paul Timothy St Clair, Deceased") == ("ST CLAIR", "PAUL", "TIMOTHY")
    assert probate.name_from_style("In the Guardianship of John Doe") is None


def test_name_text():
    assert probate.name_text(("URDA", "ARACELY", "G")) == "Aracely G Urda"


# ---------------------------------------------------------------- probate: Collin legal descriptions
def test_split_trailing_city():
    assert probate.split_city("LOT 49 BLOCK Q MIDWAY CROSSING DALLAS", "") == ("LOT 49 BLOCK Q MIDWAY CROSSING", "DALLAS")


def test_split_keeps_given_city_and_short_text():
    assert probate.split_city("LOT 49 BLOCK Q MIDWAY CROSSING DALLAS", "PLANO")[1] == "PLANO"
    assert probate.split_city("LOT 1 PLANO", "") == ("LOT 1 PLANO", "")


# ---------------------------------------------------------------- tax roll fields
def test_money():
    assert tax.money("1,234.56") == 1234.56
    assert tax.money("123456") == 1234.56          # implied cents
    assert tax.money("500.00-") == -500.0
    assert tax.money("") == 0.0


def test_dates():
    assert tax.dt("01/02/2025") == date(2025, 1, 2)
    assert tax.dt("20251102") == date(2025, 11, 2)
    assert tax.dt("1/1/9999") is None
    assert tax.dt("") is None


def test_situs():
    assert tax.situs("0001917", "PAXTON DR ") == "1917 PAXTON DR"
    assert tax.situs("0000000", "MAIN ST") == "MAIN ST"


def test_roll_date_from_file_name():
    assert tax.roll_date("Collin", ["AM103126.DAT"]) == "2026-10-31"
    assert tax.roll_date("Dallas", ["usr/flat404.DALLASCOUNTY.20261102.759348"]) == "2026-11-02"
    assert tax.roll_date("Denton", ["readme.txt"]) == ""


def test_account_numbers():
    assert tax.norm_acct("00-0420976 000") == "420976000"
    assert tax.cad_ids("Denton", {"acct": "123456DEN"}) == "123456"
    assert tax.cad_ids("Collin", {"acct": "R-1", "cad": " 2500123 "}) == "2500123"


def test_years_text():
    assert tax.years_text({"1975", "1976", "1977", "1990", "2024", "2025"}) == "1975-1977, 1990, 2024-2025"
    assert tax.years_text(set()) == ""


# ---------------------------------------------------------------- tax: archive of accounts that left the roll
HEAD = ["County", "Record type / status", "Tax account number", "Source record ID", "Original notes",
        "Lead Status", "Lead Notes", "Starred", "Email Log"]


def _row(acct, rid=None, notes="Auto-added 2026-11-07 from the roll", **app):
    r = ["Dallas", "Delinquent property tax", acct, rid or f"taxdelq:dallas:{acct}", notes, "", "", "", ""]
    for k, v in app.items():
        r[HEAD.index(k.replace("_", " "))] = v
    return r


def test_archive_candidates_only_untouched_auto_rows_off_the_roll():
    vals = [HEAD,
            _row("00011"),                                   # off the roll, untouched -> candidate
            _row("00022"),                                   # still on the roll
            _row("00033", Lead_Status="F"),                  # off the roll but someone set a status -> kept
            _row("00044", Email_Log="[11-07] Email to x"),   # emailed -> kept
            _row("00055", rid="manual-1"),                   # not added by tax.py -> never touched
            _row("00066", notes="Typed by hand"),            # notes not 'Auto-added' -> never touched
            _row("00077", rid="taxdelq:tarrant:00077")]      # other county's id -> not this tab's
    out, total, worked = tax.archive_candidates("Dallas", HEAD, vals, still={"22"})
    assert [(i, a) for i, _, a in out] == [(2, "00011")]
    assert total == 4 and worked == 2


def test_split_missing_needs_two_runs_apart():
    cand = [(2, "taxdelq:dallas:1", "1"), (3, "taxdelq:dallas:2", "2"), (4, "taxdelq:dallas:3", "3")]
    miss = [tax.MISS_HEAD,
            ["taxdelq:dallas:1", "Dallas", "1", "2026-11-03", "2026-11-03"],   # missing last week -> archive
            ["taxdelq:dallas:2", "Dallas", "2", "2026-11-09", "2026-11-09"],   # seen 1 day ago -> wait
            ["taxdelq:dallas:3", "Tarrant", "3", "2026-10-01", "2026-10-01"]]  # other county's row: ignored
    ready, keep = tax.split_missing("Dallas", cand, miss, "2026-11-10")
    assert [r[1] for r in ready] == ["taxdelq:dallas:1"] and ready[0][3] == "2026-11-03"
    assert [k[0] for k in keep] == ["taxdelq:dallas:2", "taxdelq:dallas:3"]
    assert keep[0][3] == "2026-11-09" and keep[1][3] == "2026-11-10"          # first-missing date kept / set


def test_delete_rows_batches_bottom_first():
    class Book:
        def __init__(self):
            self.calls = []

        def batch_update(self, body):
            self.calls.append(body)

    class WS:
        id = 7
        spreadsheet = Book()

    ws = WS()
    tax.delete_rows(ws, [5, 3, 4, 10, 9])
    reqs = ws.spreadsheet.calls[0]["requests"]
    spans = [(q["deleteDimension"]["range"]["startIndex"], q["deleteDimension"]["range"]["endIndex"]) for q in reqs]
    assert spans == [(8, 10), (2, 5)]                    # rows 9-10, then rows 3-5 (0-based start, end exclusive)
