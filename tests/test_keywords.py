from lib import keywords
from lib.schema import TABS
from tests.fakes import FakeWorksheet


def test_phrases_skip_fillers_and_base_words():
    p = keywords.phrases("GTA 6 Vice City POLICE CHASE is INSANE #gta6 #shorts")
    assert "police chase" in p and "vice city" in p and "insane" in p
    assert "gta 6" not in p and "is" not in p and "chase is" not in p and "shorts" not in p
    assert "पुलिस" in keywords.phrases("GTA 6 पुलिस का जलवा")


def test_mine_ranks_by_speed_and_needs_two_videos():
    recs = [{"title": "GTA 6 police chase gone wrong", "views_per_hour": 5000},
            {"title": "Insane police chase in GTA 6", "views_per_hour": 900},
            {"title": "GTA 6 secret beach location", "views_per_hour": 20000}]
    rows = keywords.mine(recs)
    phrases = [r["phrase"] for r in rows]
    assert phrases[0] == "police chase" and rows[0]["videos"] == 2
    assert "secret beach" not in phrases            # only one video
    assert "police" not in phrases and "chase" not in phrases  # covered by "police chase"


def test_trend():
    assert keywords.trend(10, "") == "new"
    assert keywords.trend(13, "10") == "rising"
    assert keywords.trend(10, "10") == "steady"
    assert keywords.trend(5, "10") == "falling"


def test_scout_writes_keywords_tab():
    from datetime import datetime, timezone
    from agents import scout

    class SS:
        def __init__(self):
            self.ws = FakeWorksheet("Keywords", [TABS["Keywords"]])

        def worksheets(self):
            return [self.ws]

        def worksheet(self, name):
            return self.ws

    ss = SS()
    recs = [{"title": "GTA 6 police chase", "views_per_hour": 100, "status": "new"},
            {"title": "police chase GTA 6 funny", "views_per_hour": 100, "status": "new"},
            {"title": "GTA 6 leak police chase", "views_per_hour": 100, "status": "blocked_leak"}]
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    rows = scout.update_keywords(ss, recs, {}, now)
    assert rows[0]["phrase"] == "police chase" and rows[0]["videos"] == 2 and rows[0]["trend"] == "new"
    rows = scout.update_keywords(ss, recs, {}, now)
    assert rows[0]["trend"] == "steady" and len(ss.ws.get_all_records()) == len(rows)
