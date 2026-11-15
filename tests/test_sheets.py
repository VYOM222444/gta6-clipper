import json

import pytest

from agents.setup_sheet import setup
from lib import sheets
from lib.schema import TABS
from tests.fakes import FakeSpreadsheet, FakeWorksheet

HEADERS = ["a", "b", "c"]


def test_creates_missing_tab():
    ss = FakeSpreadsheet([])
    assert sheets.ensure_tab(ss, "X", HEADERS) == sheets.CREATED
    ws = ss.worksheet("X")
    assert ws.row_values(1) == HEADERS
    assert ws.frozen == 1


def test_existing_correct_tab_is_ok():
    ss = FakeSpreadsheet([FakeWorksheet("X", [HEADERS, ["1", "2", "3"]])])
    assert sheets.ensure_tab(ss, "X", HEADERS) == sheets.OK


def test_empty_tab_gets_headers():
    ss = FakeSpreadsheet([FakeWorksheet("X")])
    assert sheets.ensure_tab(ss, "X", HEADERS) == sheets.HEADERS_WRITTEN
    assert ss.worksheet("X").row_values(1) == HEADERS


def test_new_columns_appended_and_data_kept():
    ws = FakeWorksheet("X", [["a", "b"], ["1", "2"]], cols=2)
    ss = FakeSpreadsheet([ws])
    assert sheets.ensure_tab(ss, "X", HEADERS) == sheets.HEADERS_EXTENDED
    assert ws.row_values(1) == HEADERS
    assert ws.row_values(2) == ["1", "2"]
    assert ws.col_count >= 3


def test_different_headers_left_untouched():
    ws = FakeWorksheet("X", [["zzz", "b"], ["1", "2"]])
    ss = FakeSpreadsheet([ws])
    assert sheets.ensure_tab(ss, "X", HEADERS) == sheets.MISMATCH
    assert ws.row_values(1) == ["zzz", "b"]


def test_full_setup_from_new_sheet_renames_sheet1():
    ss = FakeSpreadsheet([FakeWorksheet("Sheet1")])
    results = setup(ss)
    titles = [w.title for w in ss.worksheets()]
    assert "Sheet1" not in titles
    assert sorted(titles) == sorted(TABS)
    for name, headers in TABS.items():
        assert ss.worksheet(name).row_values(1) == headers
    # second run changes nothing
    again = setup(ss)
    assert all(again[name] == sheets.OK for name in TABS)
    assert results["_renamed_default_tab_to"] == "Candidates"


def test_sheet1_with_data_is_not_renamed():
    ss = FakeSpreadsheet([FakeWorksheet("Sheet1", [["my notes"]])])
    setup(ss)
    assert ss.worksheet("Sheet1").row_values(1) == ["my notes"]


def test_schema_has_no_duplicate_columns():
    for name, headers in TABS.items():
        assert len(headers) == len(set(headers)), name


@pytest.mark.parametrize(
    "value, fragment",
    [
        ("", "missing"),
        ("{not json", "not valid JSON"),
        (json.dumps({"type": "user"}), "does not look like"),
    ],
)
def test_bad_secret_gives_clear_error(value, fragment):
    with pytest.raises(sheets.SheetSetupError) as e:
        sheets.load_service_account_info({sheets.ENV_VAR: value})
    assert fragment in str(e.value)


def test_good_secret_parses():
    info = {"type": "service_account", "client_email": "bot@x.iam.gserviceaccount.com"}
    env = {sheets.ENV_VAR: json.dumps(info)}
    assert sheets.service_account_email(env) == "bot@x.iam.gserviceaccount.com"
