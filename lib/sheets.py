"""Shared Google Sheets helper. Every agent uses this instead of its own login code.

Login uses the service account JSON stored in the GitHub secret
GOOGLE_SERVICE_ACCOUNT_JSON (passed to the job as an environment variable).
The key is never printed or written to disk.
"""
import json
import os

ENV_VAR = "GOOGLE_SERVICE_ACCOUNT_JSON"

# Results of ensure_tab()
CREATED = "created"
HEADERS_WRITTEN = "headers_written"
HEADERS_EXTENDED = "headers_extended"
OK = "ok"
MISMATCH = "mismatch"


class SheetSetupError(Exception):
    """A problem the owner can fix (missing secret, sheet not shared, API off)."""


def load_service_account_info(env=None):
    env = os.environ if env is None else env
    raw = env.get(ENV_VAR, "").strip()
    if not raw:
        raise SheetSetupError(
            f"Secret {ENV_VAR} is missing. Add it in GitHub: Settings > Secrets and "
            "variables > Actions."
        )
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        raise SheetSetupError(
            f"Secret {ENV_VAR} is not valid JSON. Paste the WHOLE key file again, "
            "including the first { and last }."
        ) from None
    if info.get("type") != "service_account" or "client_email" not in info:
        raise SheetSetupError(
            f"Secret {ENV_VAR} does not look like a service account key."
        )
    return info


def get_client(env=None):
    import gspread  # imported here so tests don't need network libs loaded

    info = load_service_account_info(env)
    return gspread.service_account_from_dict(info)


def service_account_email(env=None):
    return load_service_account_info(env)["client_email"]


def open_sheet(client, sheet_id):
    import gspread

    try:
        return client.open_by_key(sheet_id)
    except gspread.exceptions.SpreadsheetNotFound:
        raise SheetSetupError(
            "Sheet not found or not shared. Share the sheet with the service account "
            "email as Editor."
        ) from None
    except gspread.exceptions.APIError as e:
        msg = str(e)
        if "has not been used" in msg or "is disabled" in msg:
            raise SheetSetupError(
                "Google Sheets API or Drive API is not enabled in your Google Cloud "
                "project. Enable both, wait 2 minutes, run again."
            ) from None
        if "PERMISSION_DENIED" in msg or "403" in msg:
            raise SheetSetupError(
                "Permission denied. Share the sheet with the service account email "
                "as Editor."
            ) from None
        raise


def ensure_tab(spreadsheet, name, headers):
    """Make sure tab `name` exists with `headers` in row 1. Never deletes data.

    Returns one of: created, headers_written, headers_extended, ok, mismatch.
    """
    titles = [ws.title for ws in spreadsheet.worksheets()]
    if name not in titles:
        ws = spreadsheet.add_worksheet(title=name, rows=1000, cols=len(headers))
        _write_headers(ws, headers)
        return CREATED

    ws = spreadsheet.worksheet(name)
    current = [h.strip() for h in ws.row_values(1)]
    while current and current[-1] == "":
        current.pop()

    if not current:
        _write_headers(ws, headers)
        return HEADERS_WRITTEN
    if current == headers:
        return OK
    if headers[: len(current)] == current:
        _write_headers(ws, headers)  # only adds new columns at the end
        return HEADERS_EXTENDED
    return MISMATCH


def _write_headers(ws, headers):
    if ws.col_count < len(headers):
        ws.add_cols(len(headers) - ws.col_count)
    ws.update(values=[headers], range_name="A1")
    ws.freeze(rows=1)
    ws.format("1:1", {"textFormat": {"bold": True}})


def read_records(ws):
    """All data rows as a list of dicts keyed by header."""
    return ws.get_all_records()


def append_rows(ws, rows):
    """Append rows (lists in schema column order)."""
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
