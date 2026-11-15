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

    def append_rows(self, rows, value_input_option=None):
        self.rows.extend([list(r) for r in rows])

    def batch_update(self, data, value_input_option=None):
        for d in data:
            cell = d["range"].split(":")[0]
            letters = "".join(ch for ch in cell if ch.isalpha())
            row_no = int("".join(ch for ch in cell if ch.isdigit()))
            col = 0
            for ch in letters:
                col = col * 26 + (ord(ch) - 64)
            row = self.rows[row_no - 1]
            vals = list(d["values"][0])
            need = col - 1 + len(vals)
            row.extend([""] * (need - len(row)))
            row[col - 1:col - 1 + len(vals)] = vals

    def get_all_records(self, numericise_ignore=None):
        if not self.rows:
            return []
        head = self.rows[0]
        return [dict(zip(head, r + [""] * (len(head) - len(r)))) for r in self.rows[1:]]


class FakeYouTube:
    """Stands in for lib.youtube.YouTube. Feed it canned API items."""

    def __init__(self, search=None, videos=None, channels=None, uploads=None):
        self.search = search or {}        # q -> [video ids]
        self._videos = {v["id"]: v for v in (videos or [])}
        self._channels = {c["id"]: c for c in (channels or [])}
        self.uploads = uploads or {}      # channel_id -> [video ids]
        self.used, self.budget = 0, 10_000
        self.calls = []

    def search_video_ids(self, q, published_after, relevance_language=None,
                         event_type=None, **kw):
        self.used += 100
        self.calls.append(("search", q, relevance_language, event_type))
        return list(self.search.get((q, event_type), self.search.get(q, [])))

    def videos(self, ids, parts=None):
        self.used += 1
        return [self._videos[i] for i in ids if i in self._videos]

    def channels(self, ids, parts=None):
        self.used += 1
        return [self._channels[i] for i in ids if i in self._channels]

    def recent_uploads(self, channel_id, max_results=10):
        self.used += 1
        return list(self.uploads.get(channel_id, []))


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
