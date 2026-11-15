"""In-memory stand-ins for gspread Spreadsheet/Worksheet (no network in tests)."""


class FakeWorksheet:
    def __init__(self, title, rows=None, cols=26):
        self.title = title
        self.rows = [list(r) for r in (rows or [])]
        self.col_count = cols
        self.frozen = 0

    def row_values(self, n):
        return list(self.rows[n - 1]) if len(self.rows) >= n else []

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def update(self, values, range_name):
        assert range_name == "A1"
        if self.rows:
            self.rows[0] = list(values[0])
        else:
            self.rows.append(list(values[0]))

    def add_cols(self, n):
        self.col_count += n

    def freeze(self, rows):
        self.frozen = rows

    def format(self, rng, fmt):
        pass

    def update_title(self, title):
        self.title = title


class FakeSpreadsheet:
    def __init__(self, worksheets):
        self._ws = list(worksheets)
        self.title = "GTA6 Clipper"

    def worksheets(self):
        return list(self._ws)

    def worksheet(self, name):
        for ws in self._ws:
            if ws.title == name:
                return ws
        raise KeyError(name)

    def add_worksheet(self, title, rows, cols):
        ws = FakeWorksheet(title, cols=cols)
        self._ws.append(ws)
        return ws
