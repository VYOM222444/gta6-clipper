from datetime import datetime, timezone

from agents import scout
from lib import sheets
from lib.config import load_rules, load_settings
from lib.schema import TABS
from tests.fakes import FakeSpreadsheet, FakeWorksheet, FakeYouTube

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
RULES = load_rules()


def video(vid, title, ch="UCa", published="2026-11-20T02:00:00Z", views=10000,
          duration="PT10M", live=None, desc="", tags=None):
    v = {
        "id": vid,
        "snippet": {"title": title, "channelId": ch, "channelTitle": ch + " name",
                    "publishedAt": published, "description": desc, "tags": tags or []},
        "statistics": {"viewCount": str(views), "likeCount": "10", "commentCount": "2"},
        "contentDetails": {"duration": duration},
    }
    if live is not None:
        v["liveStreamingDetails"] = live
    return v


def channel(cid, subs=1000, desc="", hidden=False):
    return {"id": cid, "snippet": {"description": desc},
            "statistics": {"subscriberCount": str(subs), "hiddenSubscriberCount": hidden}}


def test_parse_iso_duration():
    assert scout.parse_iso_duration("PT1H2M3S") == 3723
    assert scout.parse_iso_duration("PT45S") == 45
    assert scout.parse_iso_duration("P1DT1S") == 86401
    assert scout.parse_iso_duration("") == 0


def test_classify():
    assert scout.classify(video("a", "x", duration="PT59S"), 180) == "short"
    assert scout.classify(video("a", "x", duration="PT3M"), 180) == "short"
    assert scout.classify(video("a", "x", duration="PT3M1S"), 180) == "long"
    assert scout.classify(video("a", "x", live={"actualEndTime": "t"}), 180) == "live_vod"
    assert scout.classify(video("a", "x", live={"actualStartTime": "t"}), 180) == "live_now"


def test_is_gta6():
    p = RULES["scout"]["gta6_pattern"]
    assert scout.is_gta6(video("a", "GTA 6 cops are insane"), p)
    assert scout.is_gta6(video("a", "gta vi funny moments"), p)
    assert scout.is_gta6(video("a", "GTA6 हिंदी gameplay"), p)
    assert scout.is_gta6(video("a", "funny moments", tags=["Grand Theft Auto VI"]), p)
    assert not scout.is_gta6(video("a", "GTA 5 online heist"), p)
    assert not scout.is_gta6(video("a", "GTA 60fps settings"), p)


def test_is_leak():
    words = RULES["scout"]["leak_words"]
    assert scout.is_leak(video("a", "GTA 6 LEAKED gameplay"), words)
    assert not scout.is_leak(video("a", "GTA 6 bleak ending"), words)


def test_campaign_scan():
    cfg = RULES["campaign_lead"]
    assert scout.campaign_scan(["Clip me: https://whop.com/abc/clipping."], cfg) == (True, "https://whop.com/abc/clipping")
    assert scout.campaign_scan(["join discord.gg/xyz to clip"], cfg) == (True, "discord.gg/xyz")
    assert scout.campaign_scan(["Clippers wanted, DM me"], cfg) == (True, "")
    assert scout.campaign_scan(["subscribe for more", ""], cfg) == (False, "")


def test_score_prefers_small_channel_outliers_and_boosts():
    cfg = RULES["scout"]
    base = scout.score(1000, 0.1, cfg)
    assert scout.score(1000, 5, cfg) > base
    assert scout.score(1000, 1000, cfg) == scout.score(1000, cfg["outlier_cap"], cfg)
    assert scout.score(1000, 0.1, cfg, campaign=True) == base * cfg["campaign_boost"]


def test_build_candidates_filters_and_ranks():
    videos = [
        video("fast", "GTA 6 chase", ch="UCsmall", views=50000),
        video("slow", "GTA 6 review", ch="UCbig", views=20000),
        video("old", "GTA 6 trailer", published="2026-11-10T00:00:00Z"),
        video("other", "Minecraft build"),
        video("leak", "GTA 6 leaked map"),
        video("blocked", "GTA 6 stream", ch="UCblock"),
        video("tiny", "GTA 6 test", views=5),
    ]
    chans = [channel("UCsmall", 2000, desc="Clippers welcome: whop.com/x"),
             channel("UCbig", 1_000_000), channel("UCa", 5000), channel("UCblock")]
    recs = scout.build_candidates(videos, chans, {"fast": "GTA 6 (en)"}, set(), {"UCblock"}, RULES, NOW)
    ids = [r["video_id"] for r in recs]
    assert ids[0] == "fast"
    assert set(ids) == {"fast", "slow", "leak"}
    leak = next(r for r in recs if r["video_id"] == "leak")
    assert leak["status"] == "blocked_leak"
    fast = recs[0]
    assert fast["campaign_lead"] is True and fast["campaign_link"] == "whop.com/x"
    assert fast["age_h"] == 10.0 and fast["views_per_hour"] == 5000.0
    assert fast["outlier_score"] == 25.0
    assert fast["found_by"] == "GTA 6 (en)"
    assert fast["url"] == "https://youtu.be/fast"


def test_hidden_subscribers_and_watchlist_old_video_kept():
    v = video("w", "GTA 6 stream", ch="UCw", published="2026-11-01T00:00:00Z", views=50)
    recs = scout.build_candidates([v], [channel("UCw", hidden=True)], {}, {"UCw"}, set(), RULES, NOW)
    assert recs[0]["subscribers"] == "" and recs[0]["outlier_score"] == ""
    assert recs[0]["is_watchlist"] is True


def test_run_searches_all_queries_including_hindi_and_live():
    settings = load_settings()
    yt = FakeYouTube(
        search={"GTA 6": ["s1"], ("GTA 6", "completed"): ["live1"], "GTA 6 Hindi": ["hi1"]},
        videos=[video("s1", "GTA 6 #shorts", duration="PT30S"),
                video("live1", "GTA 6 launch stream", live={"actualEndTime": "x"}),
                video("hi1", "GTA 6 Hindi gameplay")],
        channels=[channel("UCa")],
        uploads={"UCw": ["s1"]},
    )
    watch = {"creators": [{"channel_id": "UCw", "name": "W"}]}
    recs, notes, n = scout.run(yt, settings, RULES, watch, {"channels": []}, NOW)
    assert notes == []
    searches = [c for c in yt.calls if c[0] == "search"]
    assert len(searches) == len(settings["youtube"]["searches"])
    assert ("search", "GTA 6 Hindi", "hi", None) in searches
    assert ("search", "GTA 6", "en", "completed") in searches
    kinds = {r["video_id"]: r["kind"] for r in recs}
    assert kinds == {"s1": "short", "live1": "live_vod", "hi1": "long"}
    assert n == 3


def test_upsert_keeps_found_at_and_status_no_duplicates():
    headers = TABS["Candidates"]
    ws = FakeWorksheet("Candidates", [headers])
    first = {"video_id": "a", "views": 10, "found_at": "T1", "status": "new"}
    assert sheets.upsert_rows(ws, headers, "video_id", [first]) == (0, 1)
    ws.rows[1][headers.index("status")] = "tracking"
    second = {"video_id": "a", "views": 99, "found_at": "T2", "status": "new"}
    assert sheets.upsert_rows(ws, headers, "video_id", [second, {"video_id": "b"}],
                              preserve=("found_at", "status")) == (1, 1)
    assert len(ws.rows) == 3
    row = dict(zip(headers, ws.rows[1]))
    assert row["views"] == 99 and row["found_at"] == "T1" and row["status"] == "tracking"


def test_bool_and_float_cells():
    headers = ["video_id", "is_watchlist", "x"]
    assert sheets._to_row(headers, {"video_id": "-ab", "is_watchlist": True, "x": 1.23456}) == ["-ab", "yes", 1.235]
    assert sheets._to_row(headers, {"video_id": "a", "is_watchlist": False, "x": None}) == ["a", "", ""]
