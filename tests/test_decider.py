from datetime import datetime, timedelta, timezone

from agents import decider, publish_batch
from lib.config import load_rules, load_settings

S, R = load_settings(), load_rules()
NOW = datetime(2026, 11, 23, 13, tzinfo=timezone.utc)   # 18:30 IST


def mom(mid, vid, quality=8, ct="chaos", hours_ago=3, status="ready", channel="Creator A", sat=""):
    t = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"moment_id": mid, "video_id": vid, "start_s": "100", "end_s": "125", "source": "watcher",
            "gate_status": status, "quality": str(quality), "content_type": ct, "watched_at": t,
            "reason": "cop car flips off a bridge", "hook_idea": "Cops flip off bridge",
            "title": "GTA 6 police chase stream", "channel_title": channel, "style_tags": "police chase, crash",
            "saturation": sat}


def test_ramp_by_date():
    assert decider.clips_per_brand(S, "2026-11-17") == S["batch"]["clips_per_brand"]
    assert decider.clips_per_brand(S, "2026-11-18") == 1
    assert decider.clips_per_brand(S, "2026-11-19") == 1
    assert decider.clips_per_brand(S, "2026-11-22") == 3
    assert decider.clips_per_brand(S, "2026-12-01") == 5


def test_score_uses_label_age_and_saturation():
    cfg = S["decider"]
    base = decider.moment_score(mom("a", "v"), "", NOW, cfg)
    assert decider.moment_score(mom("a", "v"), "Breakout", NOW, cfg) > base
    assert decider.moment_score(mom("a", "v", hours_ago=30), "", NOW, cfg) < base
    assert decider.moment_score(mom("a", "v", sat="2 Shorts already show it (top 5,000 views)"), "", NOW, cfg) < base


def test_pick_limits_per_video_creator_and_age():
    cfg = S["decider"]
    moments = [mom("a1", "v1", 9), mom("a2", "v1", 9), mom("a3", "v1", 9),            # 3 from one video
               mom("b1", "v2", 8, channel="Creator A"), mom("c1", "v3", 8, channel="Creator A"),
               mom("old", "v4", 10, hours_ago=60), mom("used", "v5", 10, status="in_batch"),
               mom("d1", "v6", 6, channel="Creator B")]
    cand = {"v1": {"channel_id": "UCa"}, "v2": {"channel_id": "UCa"}, "v3": {"channel_id": "UCa"},
            "v6": {"channel_id": "UCb"}}
    got = [m["moment_id"] for m in decider.pick(moments, [], cand, NOW, cfg, 10)]
    assert got == ["a1", "a2", "b1", "d1"]       # max 2 per video, max 3 per creator, no old/used


def test_assign_spreads_brands_and_types():
    picked = [{**mom(f"m{i}", f"v{i}", ct=ct), "_creator": f"c{i}"}
              for i, ct in enumerate(["chaos", "chaos", "news", "reaction", "chaos", "secret"])]
    slots = {"a": 2, "b": 2, "c": 2}
    out = decider.assign(picked, slots)
    per = {}
    for m in out:
        per.setdefault(m["_brand"], []).append(m["content_type"])
    assert sorted(len(v) for v in per.values()) == [2, 2, 2]
    assert all(v.count("chaos") <= 1 for v in per.values())


def test_finish_clip_is_always_valid():
    m = {**mom("v1-g100", "v1"), "_brand": "wasted_again"}
    texts = [
        {"hook_text": "this hook text is far too long for the screen", "title_label": "flip",
         "title": "Police chase where the cop car flips off the bridge and lands in the ocean wow",
         "what_happens": "Cop flips", "topic_hashtags": ["GTA6Police", "#GTA6", "bad tag!"],
         "ig_first_line": "x" * 200},
        decider.fallback_text(m),
    ]
    moments = {"v1-g100": {**m, "gate_status": "ready"}}
    for t in texts:
        clip = decider.finish_clip(m, t, R)
        good, bad = publish_batch.build_rows({"batch_id": "2026-11-23", "clips": [clip]}, moments, [], S, R)
        assert bad == [], bad
        assert clip["title"].lower().startswith("gta 6") and len(clip["title"]) <= 60
    clip = decider.finish_clip(m, texts[0], R)
    assert clip["hashtags"] == "#GTA6 #GTA6Shorts #Shorts #GTA6Police #badtag"
    assert clip["title_label"] == "FLIP GTA 6" or len(clip["title_label"].split()) >= 2


def test_plan_tops_up_today_and_numbers_after_existing():
    moments = [mom(f"m{i}", f"v{i}", channel=f"C{i}") for i in range(10)]
    batch_rows = [{"batch_id": "2026-11-23-01", "brand": "wasted_again", "content_type": "chaos"},
                  {"batch_id": "2026-11-22-01", "brand": "neon_wanted"}]
    batch, picked = decider.plan(moments, [], [], batch_rows, S, R, NOW)
    assert batch["batch_id"] == "2026-11-23" and batch["start_index"] == 2
    brands = [p["_brand"] for p in picked]
    assert len(picked) == 10                                   # 5/brand target, only 10 moments
    assert {2} == {brands.count(b) for b in {"neon_wanted", "busted_again", "neon_getaway", "five_star_chaos"}} \
        or brands.count("wasted_again") <= 4
    assert brands.count("wasted_again") <= 4                   # already has 1 today
    assert set(brands) == {b["key"] for b in S["brands"]}      # every brand gets clips


def test_write_text_uses_gemini_or_falls_back():
    class Gem:
        def generate_json(self, parts, prompt, schema, temperature=0.4):
            assert parts == [] and "police" in prompt and "rising word" in prompt
            return {"clips": [{"n": 1, "hook_text": "h", "title_label": "A B", "title": "GTA 6 t",
                               "what_happens": "w", "topic_hashtags": [], "ig_first_line": "i"}]}, "m"
    texts, how = decider.write_text(Gem(), [mom("a", "v")], ["rising word"], "", R)
    assert texts[1]["hook_text"] == "h" and how == "m"

    class Broken:
        def generate_json(self, *a, **k):
            from lib.gemini import GeminiError
            raise GeminiError("503")
    texts, how = decider.write_text(Broken(), [mom("a", "v")], [], "", R)
    assert texts == {} and "simple text" in how


def test_rising_keywords_and_seo():
    rows = [{"phrase": "vice beach", "score": "9", "trend": "rising", "updated_at": "2026-11-23T10:00:00Z"},
            {"phrase": "old thing", "score": "20", "trend": "rising", "updated_at": "2026-11-20T10:00:00Z"},
            {"phrase": "map", "score": "5", "trend": "falling", "updated_at": "2026-11-23T10:00:00Z"}]
    assert decider.rising_keywords(rows, NOW) == ["vice beach"]
    play = [{"section": "research.seo", "rule": "DO: title.emoji in title (winners 70% vs average 40%)"},
            {"section": "research.hook", "rule": "DO: something else"}]
    assert "emoji" in decider.seo_findings(play) and "something" not in decider.seo_findings(play)
