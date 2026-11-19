import json
from datetime import datetime, timedelta, timezone

from agents import researcher
from lib import sheets
from lib.config import load_rules, load_settings
from lib.schema import TABS
from tests.fakes import FakeWorksheet, FakeYouTube

NOW = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
S, R = load_settings(), load_rules()
CFG = S["researcher"]


def short(vid, views, days_old=5, title="GTA 6 police chase 😂", dur="PT30S", ch="UC1", desc="clip from @bigstreamer #gta6"):
    return {"id": vid, "snippet": {"title": title, "channelId": ch, "channelTitle": "Clipper",
                                   "publishedAt": (NOW - timedelta(days=days_old)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   "description": desc},
            "statistics": {"viewCount": str(views)}, "contentDetails": {"duration": dur}}


def test_score_rewards_views_and_small_channels():
    assert researcher.short_score(100_000, None) > researcher.short_score(10_000, None)
    assert researcher.short_score(10_000, 1_000) > researcher.short_score(10_000, 1_000_000)


def test_label_cohort_percentiles():
    rows = [{"score": float(i)} for i in range(20)]
    researcher.label_cohort(rows, CFG)
    by = {}
    for r in rows:
        by.setdefault(r["label"], []).append(r["score"])
    assert sorted(by["winner"]) == [17.0, 18.0, 19.0]
    assert min(by["low"]) == 0.0 and max(by["low"]) < min(by["average"])
    assert all(l in ("winner", "average", "low", "above") for l in by)


def test_build_rows_filters_long_and_non_gta6():
    vids = [short("a", 1000), short("b", 50, dur="PT5M"), short("c", 50, title="GTA 5 #gta6 clip"),
            short("d", 70)]
    rows = researcher.build_rows(vids, [{"id": "UC1", "statistics": {"subscriberCount": "100"}}], R, CFG, NOW)
    assert [r["video_id"] for r in rows] == ["a", "d"]
    assert rows[0]["credits"] == "@bigstreamer" and "#gta6" in rows[0]["hashtags"]
    assert rows[0]["age_days"] == 5.0 and rows[0]["length_s"] == 30


def test_collect_uses_date_window_and_short_duration():
    class YT(FakeYouTube):
        def search_video_ids(self, q, after, lang=None, event_type=None, **kw):
            self.calls.append(kw)
            return super().search_video_ids(q, after, lang, event_type)
    yt = YT(search={"GTA 6 #shorts": ["a", "b"]}, videos=[short("a", 1000), short("b", 10)],
            channels=[{"id": "UC1", "statistics": {"subscriberCount": "100"}}])
    rows, notes, n = researcher.collect(yt, S, R, NOW)
    assert n == 2 and len(rows) == 2 and notes == []
    kws = [c for c in yt.calls if isinstance(c, dict)]
    assert kws and all(c["duration"] == "short" and c["published_before"] for c in kws)


FEATS_W = {"hook_type": "action_first", "text_on_screen_first_second": True, "payoff_second": 1,
           "caption_style": "word_by_word", "layout": "blurred_background", "facecam": False,
           "zoom_ins": 3, "cuts": 4, "sound_effects": ["impact", "whoosh"], "background_music": True,
           "loop_ending": True, "pace": "fast", "content_type": "chaos", "topic": "police chase"}
FEATS_A = {**FEATS_W, "hook_type": "none", "text_on_screen_first_second": False, "payoff_second": 6,
           "zoom_ins": 0, "sound_effects": ["none"], "loop_ending": False, "pace": "slow",
           "topic": "map tour"}


def test_compare_finds_differences():
    out = {f["key"]: f for f in researcher.compare([FEATS_W] * 10, [FEATS_A] * 10)}
    assert out["text_on_screen_first_second"]["verdict"] == "do"
    assert out["sfx_impact"]["verdict"] == "do" and out["facecam"]["verdict"] == "same"
    assert out["zoom_ins"]["w"] == 3 and out["zoom_ins"]["a"] == 0
    assert out["hook_type=action_first"]["verdict"] == "do"
    assert out["hook_type=none"]["verdict"] == "avoid"


def research_rows(n_w=10, n_a=10):
    rows = []
    for i in range(n_w):
        rows.append({"research_id": f"w{i}", "label": "winner", "title": "GTA 6 police chase 😂 INSANE",
                     "hashtags": "#gta6 #shorts #police", "length_s": "25", "credits": "@bigstreamer",
                     "features": json.dumps(FEATS_W), "analyzed_at": "2026-11-24T10:00:00Z",
                     "collected_at": "2026-11-24T10:00:00Z"})
    for i in range(n_a):
        rows.append({"research_id": f"a{i}", "label": "average", "title": "GTA 6 map tour",
                     "hashtags": "#gta6", "length_s": "50", "credits": "",
                     "features": json.dumps(FEATS_A), "analyzed_at": "2026-11-24T10:00:00Z",
                     "collected_at": "2026-11-24T10:00:00Z"})
    return rows


def test_findings_produce_playbook_rows():
    play, src = researcher.findings(research_rows(), CFG, NOW)
    ids = {p["rule_id"]: p for p in play}
    assert "research.hook.text_on_screen_first_second" in ids
    assert ids["research.hook.text_on_screen_first_second"]["rule"].startswith("DO")
    assert ids["research.length.length_s"]["value"] == "median: winners 25 vs average 50"
    assert "research.seo.title.emoji_in_title" in ids and "research.seo.hashtag:#police" in ids
    assert "research.creators.clipped:@bigstreamer" in ids
    assert "research.today.topic:police chase" in ids
    assert "n=10 winners / 10 average" in src


def test_findings_need_enough_data():
    play, msg = researcher.findings(research_rows(3, 10), CFG, NOW)
    assert play == [] and "not enough" in msg


def test_pick_and_analyze_blind():
    rows = research_rows(0, 0) + [
        {"research_id": "w1", "label": "winner", "url": "u1", "age_days": "4", "features": ""},
        {"research_id": "w2", "label": "winner", "url": "u2", "age_days": "3", "features": ""},
        {"research_id": "a1", "label": "average", "url": "u3", "age_days": "5", "features": ""},
        {"research_id": "x", "label": "low", "url": "u4", "age_days": "5", "features": ""},
        {"research_id": "done", "label": "winner", "url": "u5", "age_days": "3", "features": "{}x"}]
    todo = researcher.pick_to_analyze(rows, 5)
    assert [r["research_id"] for r in todo] == ["w2", "a1", "w1"]

    class Gem:
        calls_this_run = 0

        def remaining(self):
            return 10

        def generate_json(self, parts, prompt, schema, temperature=0.4):
            assert "views" not in prompt.lower().split("do not judge")[0]  # blind
            if parts[0]["fileData"]["fileUri"].endswith("=a1"):
                from lib.gemini import GeminiError
                raise GeminiError("bad video")
            return FEATS_W, "flash-lite"
    upd, notes = researcher.analyze(Gem(), todo, NOW, parallel=2)
    assert upd["w2"]["model"] == "flash-lite" and upd["w2"]["hook_type"] == "action_first"
    assert json.loads(upd["w2"]["features"])["zoom_ins"] == 3
    assert "error" in json.loads(upd["a1"]["features"])        # not retried every day
    assert researcher.features_of({"features": upd["a1"]["features"]}) is None


def test_rows_round_trip_through_sheet():
    ws = FakeWorksheet("Research", [TABS["Research"]])
    rows = researcher.build_rows([short("a", 1000)], [], R, CFG, NOW)
    researcher.label_cohort(rows, CFG)
    sheets.upsert_rows(ws, TABS["Research"], "research_id", rows)
    sheets.set_values(ws, TABS["Research"], "research_id", {"a": {"features": json.dumps(FEATS_W)}})
    sheets.upsert_rows(ws, TABS["Research"], "research_id", rows)   # next day's collect
    rec = ws.get_all_records()[0]
    assert json.loads(rec["features"])["pace"] == "fast"            # analysis kept


# --- fresh tier ("right now") --------------------------------------------------------------

def fresh_short(vid, views, hours_old, title="GTA 6 police chase 😂", ch="UC1"):
    v = short(vid, views, title=title, ch=ch)
    v["snippet"]["publishedAt"] = (NOW - timedelta(hours=hours_old)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return v


def test_fresh_labels_compare_same_age_only():
    # 5 young Shorts (3 h) and 5 older (40 h). Old ones have more views but the same views/hour
    # spread, so each age bucket gets its own rising Short.
    vids = [fresh_short(f"y{i}", (i + 1) * 300, 3) for i in range(5)] + \
           [fresh_short(f"o{i}", (i + 1) * 4000, 40) for i in range(5)]
    rows = researcher.build_fresh_rows(vids, [], R, CFG, NOW)
    lab = {r["video_id"]: r["fresh_label"] for r in rows}
    assert lab["y4"] == "rising" and lab["o4"] == "rising"
    assert lab["y0"] == "fresh_low" and lab["o0"] == "fresh_low"
    assert "label" not in rows[0] and "score" not in rows[0]   # settled columns untouched
    assert rows[0]["views_per_hour"] == 100.0 and rows[0]["fresh_at"]


def test_fresh_small_buckets_merge():
    vids = [fresh_short("a", 100, 2), fresh_short("b", 5000, 30)] + \
           [fresh_short(f"o{i}", (i + 1) * 1000, 30) for i in range(4)]
    rows = researcher.build_fresh_rows(vids, [], R, CFG, NOW)
    assert len(rows) == 6 and sum(r["fresh_label"] == "rising" for r in rows) == 1


def test_fresh_window_excludes_old_and_too_new():
    vids = [fresh_short("old", 1000, 80), fresh_short("baby", 10, 0.5), fresh_short("ok", 100, 10)]
    assert [r["video_id"] for r in researcher.build_fresh_rows(vids, [], R, CFG, NOW)] == ["ok"]


def test_collect_fresh_adds_scout_shorts_without_search():
    cands = [{"video_id": "s1", "kind": "short", "published_at": "2026-11-25T02:00:00Z"},
             {"video_id": "long", "kind": "long", "published_at": "2026-11-25T02:00:00Z"},
             {"video_id": "s_old", "kind": "short", "published_at": "2026-11-20T02:00:00Z"}]
    ids = researcher.scout_short_ids(cands, NOW)
    assert ids == ["s1"]
    yt = FakeYouTube(search={"GTA 6 #shorts": ["a"]}, videos=[fresh_short("a", 500, 5), fresh_short("s1", 900, 10)],
                     channels=[])
    rows, notes, n, n_search = researcher.collect_fresh(yt, S, R, NOW, ids)
    assert {r["video_id"] for r in rows} == {"a", "s1"} and n_search == 1 and n == 2


def fresh_rows(n_w=8, n_a=8, feats_w=FEATS_W, feats_a=FEATS_A):
    rows = []
    for i in range(n_w):
        rows.append({"research_id": f"fw{i}", "fresh_label": "rising", "title": "GTA 6 cheetah chase",
                     "hashtags": "#gta6", "length_s": "20", "fresh_at": "2026-11-25T06:00:00Z",
                     "features": json.dumps({**feats_w, "topic": "cheetah chase on beach"})})
    for i in range(n_a):
        rows.append({"research_id": f"fa{i}", "fresh_label": "fresh_average", "title": "GTA 6 map",
                     "hashtags": "#gta6", "length_s": "40", "fresh_at": "2026-11-25T06:00:00Z",
                     "features": json.dumps({**feats_a, "topic": "map tour"})})
    return rows


def test_findings_now_rows_hot_words_and_shift():
    # 7-day data says loop endings win; right now the average Shorts loop and rising ones don't.
    now_w = {**FEATS_W, "loop_ending": False, "music_type": "phonk", "cuts_on_beat": True}
    now_a = {**FEATS_A, "loop_ending": True, "music_type": "none", "cuts_on_beat": False}
    rows = research_rows() + fresh_rows(feats_w=now_w, feats_a=now_a)
    play, src = researcher.findings_now(rows, CFG, NOW)
    ids = {p["rule_id"]: p for p in play}
    assert "n=8 rising / 8 average" in src
    assert ids["research.now.sound.music_type=phonk"]["rule"].startswith("DO")
    assert ids["research.now.sound.cuts_on_beat"]["rule"].startswith("DO")
    assert "research.now.topics.word:cheetah" in ids and "research.now.topics.word:map" not in ids
    loop = ids["research.now.editing.loop_ending"]["rule"]
    assert loop.startswith("AVOID") and "SHIFT (7-day: DO)" in loop
    # settled findings ignore fresh rows and new music fields that old rows lack
    settled, _ = researcher.findings(rows, CFG, NOW)
    assert not any(p["rule_id"].startswith("research.now") for p in settled)
    assert not any("music_type" in p["rule_id"] for p in settled)


def test_findings_now_need_enough_and_recent():
    play, msg = researcher.findings_now(fresh_rows(3, 8), CFG, NOW)
    assert play == [] and "not enough" in msg
    old = [{**r, "fresh_at": "2026-11-20T06:00:00Z"} for r in fresh_rows()]
    assert researcher.findings_now(old, CFG, NOW)[0] == []


def test_pick_fresh_only_recent_unanalysed():
    rows = [{"research_id": "r1", "fresh_label": "rising", "fresh_at": "2026-11-25T10:00:00Z", "age_days": "1",
             "views_per_hour": "50", "features": ""},
            {"research_id": "r2", "fresh_label": "rising", "fresh_at": "2026-11-25T10:00:00Z", "age_days": "1",
             "views_per_hour": "90", "features": ""},
            {"research_id": "a1", "fresh_label": "fresh_average", "fresh_at": "2026-11-25T10:00:00Z",
             "age_days": "2", "views_per_hour": "5", "features": ""},
            {"research_id": "stale", "fresh_label": "rising", "fresh_at": "2026-11-23T10:00:00Z",
             "age_days": "1", "views_per_hour": "500", "features": ""},
            {"research_id": "done", "fresh_label": "rising", "fresh_at": "2026-11-25T10:00:00Z",
             "age_days": "1", "views_per_hour": "500", "features": "{\"a\":1}"}]
    assert [r["research_id"] for r in researcher.pick_fresh(rows, 5, NOW)] == ["r2", "a1", "r1"]


def test_fresh_row_upsert_keeps_settled_label():
    ws = FakeWorksheet("Research", [TABS["Research"]])
    sheets.upsert_rows(ws, TABS["Research"], "research_id",
                       [{"research_id": "x", "video_id": "x", "label": "winner", "score": 5}])
    sheets.upsert_rows(ws, TABS["Research"], "research_id",
                       researcher.build_fresh_rows([fresh_short("x", 900, 10)] * 1, [], R, CFG, NOW))
    rec = ws.get_all_records()[0]
    assert rec["label"] == "winner" and str(rec["score"]) == "5" and rec["fresh_label"]
