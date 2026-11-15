from datetime import datetime, timezone

from agents import watcher
from lib.config import load_rules

RULES = load_rules()
NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)


def cand(vid, kind="long", score="10", dur="900", title="GTA 6 police chase", status="new"):
    return {"video_id": vid, "kind": kind, "score": score, "duration_s": dur, "title": title,
            "status": status, "channel_title": "Creator", "channel_id": "UC" + vid}


def sig(vid, start, end, strength, status="pending", signal="chat"):
    return {"moment_id": f"{vid}-{start}", "video_id": vid, "start_s": str(start), "end_s": str(end),
            "source": "signal", "signal": signal, "strength": str(strength), "gate_status": status}


def test_pick_jobs_signals_first_then_whole_videos():
    cands = [cand("a", kind="live_vod", dur="20000"), cand("b", score="99"), cand("c", dur="5000"),
             cand("d", status="watched"), cand("e", title="GTA 5 #gta6"), cand("f", kind="short", dur="30")]
    moments = [sig("a", 100, 160, 3), sig("a", 900, 950, 8), sig("a", 50, 80, 1, status="watched"),
               sig("zz", 1, 2, 9)]
    jobs = watcher.pick_jobs(cands, moments, RULES)
    assert [j["candidate"]["video_id"] for j in jobs] == ["a", "b"]
    assert jobs[0]["windows"] == [(100.0, 160.0), (900.0, 950.0)]
    assert jobs[1]["windows"] == []


def test_max_windows_per_call():
    moments = [sig("a", i * 100, i * 100 + 50, i) for i in range(9)]
    job = watcher.pick_jobs([cand("a")], moments, RULES)[0]
    assert len(job["windows"]) == RULES["watcher"]["max_windows_per_call"]
    assert job["windows"][0][0] == 400.0   # strongest 5 kept, sorted by time


def test_prompt_lists_segments():
    p = watcher.build_prompt(cand("a"), [(100, 160), (3700, 3760)], RULES)
    assert "segment 0: 1:40-2:40" in p and "segment 1: 1:01:40-1:02:40" in p
    assert "START OF THAT SEGMENT" in p
    assert "whole video" in watcher.build_prompt(cand("a"), [], RULES)


def test_to_absolute_relative_and_absolute_answers():
    w = [(100.0, 160.0), (900.0, 950.0)]
    assert watcher.to_absolute({"segment": 1, "start": 5, "end": 25}, w) == (905.0, 925.0)
    assert watcher.to_absolute({"segment": 0, "start": 110, "end": 140}, w) == (110.0, 140.0)
    assert watcher.to_absolute({"segment": 7, "start": 1, "end": 2}, w) is None
    assert watcher.to_absolute({"segment": 0, "start": 30, "end": 50}, []) == (30.0, 50.0)


def test_fit_length():
    cfg = RULES["watcher"]
    assert watcher.fit_length(10, 200, cfg) == (10, 10 + cfg["clip_max_seconds"])
    assert watcher.fit_length(10, 12, cfg) == (10, 10 + cfg["clip_min_seconds"])
    assert watcher.fit_length(-3, 20, cfg) == (0, 20)


def gm(**kw):
    m = {"segment": 0, "start": 5, "end": 30, "what_happens": "cop car flips", "content_type": "chaos",
         "style_tags": ["police chase"], "quality": 8, "standalone": True, "has_promo": False,
         "hook_idea": "GTA 6 cops are insane"}
    m.update(kw)
    return m


def test_moments_from_answer_gates():
    job = {"candidate": cand("a"), "windows": [(100.0, 160.0)], "signal_rows": [sig("a", 100, 160, 5, signal="chat+comments")]}
    answer = {"moments": [gm(), gm(start=40, end=55, has_promo=True), gm(start=10, end=30, quality=3),
                          gm(start=50, end=59, standalone=False)]}
    used = {("a", 500.0)}
    rows = watcher.moments_from_answer(answer, job, used, set(), RULES, NOW)
    status = [r["gate_status"] for r in rows]
    assert status == ["ready", "rejected_promo", "rejected_low", "rejected_context"]
    r = rows[0]
    assert r["moment_id"] == "a-g105" and r["start_s"] == 105 and r["end_s"] == 130
    assert r["source"] == "watcher" and r["signal"] == "chat+comments"
    assert r["url"] == "https://youtu.be/a?t=105" and r["style_tags"] == "police chase"


def test_used_and_blocklist():
    job = {"candidate": cand("a"), "windows": [], "signal_rows": []}
    rows = watcher.moments_from_answer({"moments": [gm(start=100, end=120)]}, job, {("a", 110.0)}, set(), RULES, NOW)
    assert rows[0]["gate_status"] == "rejected_used" and rows[0]["signal"] == "whole"
    rows = watcher.moments_from_answer({"moments": [gm()]}, job, set(), {"UCa"}, RULES, NOW)
    assert rows[0]["gate_status"] == "rejected_blocklist"


def test_used_spans_from_batch_and_posted():
    moments = [{"moment_id": "m1", "video_id": "v", "start_s": "40"}]
    spans = watcher.used_spans([{"video_id": "b", "start_s": "10"}], [{"moment_id": "m1"}], moments)
    assert spans == {("b", 10.0), ("v", 40.0)}


class FakeGem:
    def __init__(self, answers, budget=10):
        self.answers, self.calls_this_run, self.budget, self.used = answers, 0, budget, 0

    def remaining(self):
        return self.budget - self.used

    def generate_json(self, parts, prompt, schema):
        self.calls_this_run += 1
        self.used += 1
        return self.answers.pop(0), "gemini-test"


def test_run_respects_per_run_cap_and_marks_watched():
    jobs = watcher.pick_jobs([cand("a"), cand("b"), cand("c")], [sig("a", 100, 160, 5)], RULES)
    gem = FakeGem([{"moments": [gm()]}, {"moments": []}, {"moments": [gm()]}])
    rows, wsig, wvid, notes = watcher.run(gem, jobs, set(), set(), RULES, 2, NOW)
    assert gem.calls_this_run == 2
    assert wsig == {"a-100": "watched"} and set(wvid) == {"a", "b"}
    assert len(rows) == 1
