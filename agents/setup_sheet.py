"""Create the 7 dashboard tabs (with headers) in the "GTA6 Clipper" sheet.

Safe to re-run: existing tabs and data are never deleted or overwritten. New columns added
to lib/schema.py are appended to the end of the header row.

Run:  python -m agents.setup_sheet
Needs env var GOOGLE_SERVICE_ACCOUNT_JSON (GitHub secret).
"""
import sys

from lib import sheets
from lib.config import load_settings
from lib.schema import TABS


def reuse_blank_default_tab(spreadsheet):
    """A new Google Sheet starts with an empty "Sheet1". Rename it to the first missing
    tab instead of leaving it lying around. Returns the new name, or None."""
    worksheets = spreadsheet.worksheets()
    titles = [ws.title for ws in worksheets]
    missing = [name for name in TABS if name not in titles]
    if not missing:
        return None
    for ws in worksheets:
        if ws.title not in TABS and not any(any(c for c in row) for row in ws.get_all_values()):
            ws.update_title(missing[0])
            return missing[0]
    return None


def setup(spreadsheet):
    """Returns {tab_name: result}."""
    results = {}
    renamed = reuse_blank_default_tab(spreadsheet)
    if renamed:
        results["_renamed_default_tab_to"] = renamed
    for name, headers in TABS.items():
        results[name] = sheets.ensure_tab(spreadsheet, name, headers)
    return results


def main():
    settings = load_settings()
    try:
        email = sheets.service_account_email()
        print(f"Service account: {email}")
        client = sheets.get_client()
        spreadsheet = sheets.open_sheet(client, settings["sheet"]["id"])
        print(f"Opened sheet: {spreadsheet.title}")
        results = setup(spreadsheet)
    except sheets.SheetSetupError as e:
        print(f"SETUP FAILED: {e}")
        return 1

    print("Tabs:")
    for name, result in results.items():
        print(f"  {name}: {result}")

    mismatches = [n for n, r in results.items() if r == sheets.MISMATCH]
    if mismatches:
        print(
            "WARNING: these tabs have different headers than lib/schema.py and were "
            f"left untouched: {', '.join(mismatches)}. Fix the header row by hand or "
            "rename the tab."
        )
        return 1
    print("Connection OK. All tabs ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
