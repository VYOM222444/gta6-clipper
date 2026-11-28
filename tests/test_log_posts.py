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


def test_match_uploads_by_title():
    from agents.log_posts import match_uploads
    batch = [{"batch_id": "2026-11-24-01", "brand": "wasted_again", "title": "GTA 6 cop chase ends in a lake",
              "moment_id": "m1"},
             {"batch_id": "2026-11-24-02", "brand": "wasted_again", "title": "GTA 6 bike crash off the pier",
              "moment_id": "m2"},
             {"batch_id": "2026-11-24-03", "brand": "neon_wanted", "title": "GTA 6 cop chase ends in a lake"}]
    uploads = [{"id": "AAAAAAAAAAA", "snippet": {"title": "GTA 6 Cop Chase Ends In A Lake 😂 #shorts",
                                                 "publishedAt": "2026-11-24T15:00:00Z"}},
               {"id": "BBBBBBBBBBB", "snippet": {"title": "my channel trailer", "publishedAt": "2026-11-24T15:00:00Z"}},
               {"id": "CCCCCCCCCCC", "snippet": {"title": "GTA 6 bike crash off the pier",
                                                 "publishedAt": "2026-11-20T15:00:00Z"}}]  # before batch day
    rows = match_uploads(uploads, batch, "wasted_again", [])
    assert [(r["batch_id"], r["platform_id"]) for r in rows] == [("2026-11-24-01", "AAAAAAAAAAA")]
    assert rows[0]["post_id"] == "2026-11-24-01-youtube" and rows[0]["moment_id"] == "m1"
    # already logged -> nothing new
    assert match_uploads(uploads, batch, "wasted_again", [{"platform": "youtube", "batch_id": "2026-11-24-01",
                                                           "platform_id": "AAAAAAAAAAA"}]) == []


# --- Instagram + Facebook numbers (Meta Graph API) ---------------------------------------------

from agents import post_social  # noqa: E402

META_NOW = datetime(2026, 11, 28, 15, tzinfo=timezone.utc)
TOKEN = "SECRET-PAGE-TOKEN"


class FakeMeta:
    """Graph API stand-in: answers[path] = dict, an Exception to raise, or fn(params) -> either."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def get(self, path, **params):
        self.calls.append((path, params))
        a = self.answers.get(path)
        a = a(params) if callable(a) else a
        if isinstance(a, Exception):
            raise a
        if a is None:
            raise post_social.MetaError(f"HTTP 400: (#803) unknown object {path}")
        return a


def ins(**values):
    return {"data": [{"name": k, "period": "lifetime", "values": [{"value": v}]} for k, v in values.items()]}


def posted_ws(rows):
    head = TABS["Posted"]
    return FakeWorksheet("Posted", [head] + [[str(r.get(h, "")) for h in head] for r in rows])


SEVEN_H_AGO = "2026-11-28T08:00:00Z"
META_ROWS = [
    {"post_id": "2026-11-28-01-instagram", "batch_id": "2026-11-28-01", "brand": "wasted_again",
     "platform": "instagram", "platform_id": "17900000000000001", "posted_at": SEVEN_H_AGO,
     "url": "https://www.instagram.com/reel/AbCdEf123/"},
    {"post_id": "2026-11-16-05-instagram", "batch_id": "2026-11-16-05", "brand": "wasted_again",
     "platform": "instagram", "platform_id": "DfzjGjHTrRf", "posted_at": "2026-11-16T14:00:00Z",
     "url": "https://www.instagram.com/reel/DfzjGjHTrRf/"},                     # pasted by hand
    {"post_id": "2026-11-28-01-facebook", "batch_id": "2026-11-28-01", "brand": "wasted_again",
     "platform": "facebook", "platform_id": "1093000000000001", "posted_at": SEVEN_H_AGO},
    {"post_id": "2026-11-28-02-instagram", "batch_id": "2026-11-28-02", "brand": "neon_wanted",
     "platform": "instagram", "platform_id": "17900000000000009", "posted_at": SEVEN_H_AGO},   # no token
    {"post_id": "2026-11-28-01-youtube", "batch_id": "2026-11-28-01", "brand": "wasted_again",
     "platform": "youtube", "platform_id": "FvMOhbJz_IE", "posted_at": SEVEN_H_AGO},
]


def meta_answers():
    return {
        "17900000000000001": {"like_count": 12, "comments_count": 3, "timestamp": "2026-11-28T08:00:00+0000"},
        "17900000000000001/insights": ins(views=700, reach=500, saved=4, shares=6, ig_reels_avg_watch_time=6400),
        "ig1/media": {"data": [{"id": "17900000000000002", "permalink": "https://www.instagram.com/reel/DfzjGjHTrRf/"}]},
        "17900000000000002": {"like_count": 40, "comments_count": 5, "timestamp": "2026-11-16T14:20:00+0000"},
        "17900000000000002/insights": ins(views=2890, reach=2000, saved=1, shares=2, ig_reels_avg_watch_time=5100),
        "1093000000000001": {"created_time": "2026-11-28T08:00:00+0000",
                             "comments": {"data": [], "summary": {"total_count": 2}},
                             "likes": {"data": [], "summary": {"total_count": 9}}},
        "1093000000000001/video_insights": ins(fb_reels_total_plays=350, post_impressions_unique=300,
                                               post_video_avg_time_watched=4200, post_video_social_actions=5,
                                               post_video_likes_by_reaction_type={"REACTION_LIKE": 8,
                                                                                  "REACTION_LOVE": 1}),
    }


def test_meta_stats_fills_instagram_and_facebook_numbers():
    ws = posted_ws(META_ROWS)
    fake = FakeMeta(meta_answers())
    tokens = {"wasted_again": {"token": TOKEN, "page_id": "p1", "ig_user_id": "ig1"}}
    counts, notes = log_posts.meta_stats(ws, {}, META_NOW, tokens=tokens, make_meta=lambda t: fake)
    assert counts == {"instagram": 2, "facebook": 1} and notes == []
    rows = {r["post_id"]: r for r in ws.get_all_records()}
    ig = rows["2026-11-28-01-instagram"]
    assert (ig["views_now"], ig["likes_now"], ig["comments_now"]) == (700, 12, 3)
    assert (ig["reach_now"], ig["shares_now"], ig["saves_now"], ig["avg_watch_s"]) == (500, 6, 4, 6.4)
    assert ig["views_6h"] == 600 and ig["views_12h"] == "" and ig["stats_at"] == "2026-11-28T15:00:00Z"
    old = rows["2026-11-16-05-instagram"]                       # found by its shortcode
    assert old["views_now"] == 2890 and old["posted_at"] == "2026-11-16T14:20:00Z"
    assert old["views_7d"] == round(2890 * 168 / 288.67) or abs(old["views_7d"] - 2890 * 168 / 288.67) < 2
    assert old["likes_7d"] == 40 and old["views_24h"] == ""     # too far back for an honest 24 h value
    fb = rows["2026-11-28-01-facebook"]
    assert (fb["views_now"], fb["likes_now"], fb["comments_now"], fb["shares_now"]) == (350, 9, 2, 3)
    assert (fb["reach_now"], fb["avg_watch_s"], fb["saves_now"]) == (300, 4.2, "")
    assert rows["2026-11-28-02-instagram"]["views_now"] == ""   # brand without a token: untouched
    assert rows["2026-11-28-01-youtube"]["views_now"] == ""     # YouTube is not Meta's job
    assert [p for p, _ in fake.calls].count("ig1/media") == 1   # shortcode lookup once per brand


def test_meta_stats_drops_a_metric_meta_no_longer_knows():
    a = meta_answers()

    def renamed(params):
        if "," in params["metric"]:
            return post_social.MetaError("HTTP 400: (#100) metric[4] must be one of the following values: views")
        return ins(views=111)
    a["17900000000000001/insights"] = renamed
    a["17900000000000002/insights"] = renamed
    ws, fake = posted_ws(META_ROWS[:2]), FakeMeta(a)
    tokens = {"wasted_again": {"token": TOKEN, "page_id": "p1", "ig_user_id": "ig1"}}
    counts, notes = log_posts.meta_stats(ws, {}, META_NOW, tokens=tokens, make_meta=lambda t: fake)
    assert counts["instagram"] == 2 and notes == []
    assert all(r["views_now"] == 111 for r in ws.get_all_records())
    tried = [p["metric"] for path, p in fake.calls if path.endswith("/insights")]
    assert tried == [",".join(log_posts.IG_SETS[0]), "views", "views"]   # learned once, kept for the run


def test_meta_stats_without_insight_permission_keeps_likes_and_says_what_to_do():
    a = meta_answers()
    a["17900000000000001/insights"] = post_social.MetaError(
        "HTTP 403: (#10) Application does not have permission for this action")
    ws, fake = posted_ws(META_ROWS[:2]), FakeMeta(a)
    tokens = {"wasted_again": {"token": TOKEN, "page_id": "p1", "ig_user_id": "ig1"}}
    counts, notes = log_posts.meta_stats(ws, {}, META_NOW, tokens=tokens, make_meta=lambda t: fake)
    rows = {r["post_id"]: r for r in ws.get_all_records()}
    assert rows["2026-11-28-01-instagram"]["likes_now"] == 12
    assert rows["2026-11-28-01-instagram"]["views_now"] == "" and rows["2026-11-28-01-instagram"]["stats_at"] == ""
    assert any("instagram_manage_insights" in n for n in notes)
    assert sum(1 for path, _ in fake.calls if path.endswith("/insights")) == 1   # not asked again


def test_meta_stats_rate_limit_stops_that_brand_and_never_prints_the_token():
    a = meta_answers()
    a["17900000000000001"] = post_social.MetaError(f"HTTP 400: (#4) Application request limit reached {TOKEN}")
    ws, fake = posted_ws(META_ROWS), FakeMeta(a)
    tokens = {"wasted_again": {"token": TOKEN, "page_id": "p1", "ig_user_id": "ig1"},
              "neon_wanted": {"token": "OTHER", "page_id": "p2", "ig_user_id": "ig2"}}
    a["17900000000000009"] = {"like_count": 1, "comments_count": 0, "timestamp": "2026-11-28T08:00:00+0000"}
    a["17900000000000009/insights"] = ins(views=50)
    counts, notes = log_posts.meta_stats(ws, {}, META_NOW, tokens=tokens, make_meta=lambda t: fake)
    assert counts == {"instagram": 1, "facebook": 0}               # neon_wanted still read
    assert any("rate limit" in n for n in notes) and not any(TOKEN in n for n in notes)


def test_meta_stats_not_set_up():
    assert log_posts.meta_stats(posted_ws(META_ROWS), {}, META_NOW, tokens={})[0] is None


def test_insight_values_and_error_kinds():
    assert log_posts.insight_values({"data": [
        {"name": "views", "values": [{"value": 5}]},
        {"name": "reach", "total_value": {"value": 4}},
        {"name": "post_video_likes_by_reaction_type", "values": [{"value": {"REACTION_LIKE": 2, "REACTION_WOW": 1}}]},
        {"name": "empty", "values": []}]}) == {"views": 5, "reach": 4, "post_video_likes_by_reaction_type": 3}
    assert log_posts.error_kind("HTTP 400: (#100) metric[0] must be one of the following values") == "metric"
    assert log_posts.error_kind("HTTP 403: (#200) Requires instagram_manage_insights permission") == "permission"
    assert log_posts.error_kind("HTTP 400: (#17) User request limit reached") == "limit"
    assert log_posts.error_kind("HTTP 400: (#100) Unsupported get request") == ""


def test_main_stats_reads_meta_numbers_with_the_pasted_accounts_answer(monkeypatch):
    """End to end: META_TOKENS_JSON = Meta's me/accounts answer, `log_posts stats` fills Instagram views."""
    from lib import sheets as _sheets
    from tests.fakes import FakeSpreadsheet

    class YT(FakeYouTube):
        def channel_id_for_handle(self, handle):
            return None
    ss = FakeSpreadsheet([posted_ws(META_ROWS[:1]), FakeWorksheet("Batch", [TABS["Batch"]]),
                          FakeWorksheet("Playbook", [TABS["Playbook"]])])
    fake = FakeMeta(meta_answers())
    monkeypatch.setenv("META_TOKENS_JSON", json.dumps({"data": [
        {"name": "Wasted Again Clips", "id": "p1", "access_token": TOKEN,
         "instagram_business_account": {"id": "ig1"}}], "paging": {}}))
    monkeypatch.setattr(_sheets, "get_client", lambda: None)
    monkeypatch.setattr(_sheets, "open_sheet", lambda c, i: ss)
    monkeypatch.setattr(log_posts, "YouTube", lambda **k: YT())
    monkeypatch.setattr(post_social, "Meta", lambda token, version="v25.0": fake)
    assert log_posts.main(["stats"]) == 0
    row = ss.worksheet("Posted").get_all_records()[0]
    assert row["views_now"] == 700 and row["avg_watch_s"] == 6.4
