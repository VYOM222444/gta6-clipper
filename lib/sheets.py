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


SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]


def in_colab():
    try:
        import google.colab  # noqa: F401
        return True
    except ImportError:
        return False


def get_client(env=None):
    """Service account on GitHub; in Colab (no secret set) the owner's own Google login."""
    import gspread  # imported here so tests don't need network libs loaded

    env = os.environ if env is None else env
    if not env.get(ENV_VAR, "").strip() and in_colab():
        import google.auth
        from google.colab import auth

        auth.authenticate_user()
        creds, _ = google.auth.default(scopes=SCOPES)
        return gspread.authorize(creds)
    info = load_service_account_info(env)
    return gspread.service_account_from_dict(info)


def service_account_email(env=None):
    return load_service_account_info(env)["client_email"]


def open_sheet(client, sheet_id):
    import gspread

    try:
        return client.open_by_key(sheet_id)
    except PermissionError:  # gspread 6 raises this on 403 in open_by_key
        raise SheetSetupError(
            "Permission denied. Share the 'GTA6 Clipper' sheet with the service account "
            "email (printed above) as Editor."
        ) from None
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
    """All data rows as a list of dicts keyed by header. Values stay strings, so ids like
    "0123" or "1e5" are not turned into numbers."""
    return ws.get_all_records(numericise_ignore=["all"])


def append_rows(ws, rows):
    """Append rows (lists in schema column order)."""
    if rows:
        ws.append_rows(rows, value_input_option="RAW")


def _col_letter(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def upsert_rows(ws, headers, key, records, preserve=()):
    """Insert or update rows keyed by column `key`. `records` are dicts.

    Existing rows are rewritten in place (no duplicates); for columns in `preserve`, a
    non-empty value already in the sheet is kept (e.g. found_at, status). Columns a record
    doesn't mention at all keep their old value (another agent may own them).
    Values are written RAW (a video id starting with "-" or "=" must never become a
    formula). Returns (updated_count, appended_count).
    """
    values = ws.get_all_values()
    key_idx = headers.index(key)
    row_of = {}
    for i, row in enumerate(values[1:], start=2):
        if len(row) > key_idx and row[key_idx]:
            row_of[row[key_idx]] = (i, row)

    last = _col_letter(len(headers))
    updates, appends = [], []
    for rec in records:
        k = str(rec[key])
        if k in row_of:
            i, old_row = row_of[k]
            old = dict(zip(headers, old_row))
            merged = {h: old.get(h, "") for h in headers if h not in rec}
            merged.update(rec)
            for col in preserve:
                if old.get(col, "") != "":
                    merged[col] = old[col]
            updates.append({"range": f"A{i}:{last}{i}", "values": [_to_row(headers, merged)]})
        else:
            appends.append(_to_row(headers, rec))
            row_of[k] = (None, None)  # avoid duplicate appends within one call

    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    if appends:
        ws.append_rows(appends, value_input_option="RAW")
    return len(updates), len(appends)


def set_column(ws, headers, key, column, values_by_key):
    """Set one column for the rows whose `key` is in values_by_key. Returns count."""
    rows = ws.get_all_values()
    ki, ci = headers.index(key), headers.index(column)
    col = _col_letter(ci + 1)
    updates = []
    for i, row in enumerate(rows[1:], start=2):
        k = row[ki] if len(row) > ki else ""
        if k in values_by_key:
            updates.append({"range": f"{col}{i}", "values": [[values_by_key[k]]]})
    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    return len(updates)


def set_values(ws, headers, key, updates_by_key):
    """Set several columns for several rows: {key: {column: value}}. One read, one write."""
    rows = ws.get_all_values()
    ki = headers.index(key)
    data = []
    for i, row in enumerate(rows[1:], start=2):
        k = row[ki] if len(row) > ki else ""
        for col, val in (updates_by_key.get(k) or {}).items():
            data.append({"range": f"{_col_letter(headers.index(col) + 1)}{i}", "values": [[val]]})
    if data:
        ws.batch_update(data, value_input_option="RAW")
    return len(data)


def _to_row(headers, rec):
    row = []
    for h in headers:
        v = rec.get(h, "")
        if v is None:
            v = ""
        elif isinstance(v, bool):
            v = "yes" if v else ""
        elif isinstance(v, float):
            v = round(v, 3)
        row.append(v)
    return row
