import json
from datetime import datetime, timezone

from agents import log_posts
from lib import sheets
from lib.config import REPO_ROOT
from lib.schema import TABS
from tests.fakes import FakeWorksheet, FakeYouTube

NOW = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)


def test_platform_ids():
    assert log_posts.platform_id("youtube", "https://youtube.com/shorts/FvMOhbJz_IE?si=Z9") == "FvMOhbJz_IE"
    assert log_posts.platform_id("youtube", "https://youtu.be/abcdefghijk") == "abcdefghijk"
    assert log_posts.platform_id("instagram", "https://www.instagram.com/reel/DfzjGjHTrRf/?vrfl=x") == "DfzjGjHTrRf"


def test_first_posts_file_is_valid():
    data = json.loads((REPO_ROOT / "posts" / "2026-11-16.json").read_text())
    rows = log_posts.rows_from_file(data, [{"batch_id": "2026-11-16-02", "moment_id": "BA23aWqUV0I-g713"}])
    assert len(rows) == 10 and len({r["post_id"] for r in rows}) == 10
    assert all(r["platform_id"] for r in rows)
    five = next(r for r in rows if r["post_id"] == "2026-11-16-02-youtube")
    assert five["moment_id"] == "BA23aWqUV0I-g713" and five["brand"] == "five_star_chaos"
    assert "?" not in five["url"]


def video(vid, views, published="2026-11-16T14:05:00Z"):
    return {"id": vid, "snippet": {"publishedAt": published},
            "statistics": {"viewCount": str(views), "likeCount": "7", "commentCount": "2"}}


def test_checkpoint_value_interpolates():
    assert log_posts.checkpoint_value(6, 4, 100) is None                    # not reached
    assert log_posts.checkpoint_value(6, 10, 1000, 4, 200) == 467
    assert log_posts.checkpoint_value(24, 30, 3000) == 2400                 # from 0 at posting
    assert log_posts.checkpoint_value(6, 30, 3000) is None                  # too far: blank


def test_update_stats_checkpoints():
    row = {"views_24h": "", "views_7d": ""}
    u = log_posts.update_stats(row, video("x", 500, "2026-11-24T00:00:00Z"), NOW)   # 15 h old
    assert u["views_now"] == 500 and "views_24h" not in u
    assert u["views_12h"] == 400 and "views_6h" not in u                     # 6 h: too far back
    prev = {"stats_at": "2026-11-24T09:00:00Z", "views_now": "300"}          # read at 9 h
    u = log_posts.update_stats(prev, video("x", 500, "2026-11-24T00:00:00Z"), NOW)
    assert u["views_12h"] == 400 and "views_6h" not in u
    u = log_posts.update_stats({"views_24h": "900", "views_7d": "", "views_48h": "1", "views_6h": "1",
                                "views_12h": "1"}, video("x", 4000), NOW)  # 8 days, first reading
    assert "views_24h" not in u and abs(u["views_7d"] - 4000 * 168 / 192.9) < 5 and u["likes_7d"] == 7
    assert u["posted_at"] == "2026-11-16T14:05:00Z"


def test_log_then_stats_end_to_end(tmp_path):
    ws = FakeWorksheet("Posted", [TABS["Posted"]])
    upd, new = log_posts.log(ws, [], [str(REPO_ROOT / "posts" / "2026-11-16.json")])
    assert (upd, new) == (0, 10)
    assert log_posts.log(ws, [], [str(REPO_ROOT / "posts" / "2026-11-16.json")]) == (10, 0)
    yt = FakeYouTube(videos=[video("FvMOhbJz_IE", 1234)])
    assert log_posts.stats(ws, yt, NOW) == 1
    row = next(r for r in ws.get_all_records() if r["post_id"] == "2026-11-16-05-youtube")
    assert row["views_now"] == 1234 and row["views_7d"] > 1000 and row["views_24h"] == ""
    # re-logging keeps the numbers
    log_posts.log(ws, [], [str(REPO_ROOT / "posts" / "2026-11-16.json")])
    row = next(r for r in ws.get_all_records() if r["post_id"] == "2026-11-16-05-youtube")
    assert row["views_now"] == 1234
