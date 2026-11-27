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
    assert watcher.fit_length(10, 200, 15, 30) == (10, 40)
    assert watcher.fit_length(10, 25, 15, 30) == (10, 25)          # already long enough
    assert watcher.fit_length(10, 15, 15, 30) == (7, 22)            # 3 s lead-in, rest after
    assert watcher.fit_length(10, 13, 15, 30) == (7, 22)
    assert watcher.fit_length(1, 5, 15, 30) == (0, 15)              # can't go before 0
    assert watcher.fit_length(-3, 20, 15, 30) == (0, 20)


def test_clip_range_per_content_type():
    assert watcher.clip_range("chaos", RULES) == tuple(RULES["clip_length_seconds"]["chaos"])
    w = RULES["watcher"]
    assert watcher.clip_range("other", RULES) == (w["clip_min_seconds"], w["clip_max_seconds"])


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


def test_run_parallel_waves_failures_and_time_limit():
    import copy
    from lib.gemini import GeminiError, Transient

    class MixedGem(FakeGem):
        def generate_json(self, parts, prompt, schema):
            self.calls_this_run += 1
            self.used += 1
            vid = parts[0]["fileData"]["fileUri"].split("=")[-1]
            if vid == "a":
                raise Transient("503 busy")
            if vid == "b":
                raise GeminiError("bad request")
            return {"moments": [gm()]}, "m"
    rules = copy.deepcopy(RULES)
    rules["watcher"]["parallel_calls"] = 2
    jobs = watcher.pick_jobs([cand("a", score="9"), cand("b", score="8"), cand("c", score="7"),
                              cand("d", score="6")], [], rules)
    ticks = iter([0, 0, 30 * 60])
    rows, wsig, wvid, notes = watcher.run(MixedGem([]), jobs, set(), set(), rules, 8, NOW,
                                          max_minutes=22, clock=lambda: next(ticks))
    assert wvid["a"] == "watch_retry"           # busy: retried next run
    assert wvid["b"] == "watch_failed"
    assert "c" not in wvid and "d" not in wvid  # second wave never started (time limit)
    assert any("stopped after 22 min" in n for n in notes)


def test_timeout_becomes_gemini_error(monkeypatch):
    import socket
    from lib import gemini

    def boom(*a, **k):
        raise socket.timeout("timed out")
    monkeypatch.setattr(gemini.urllib.request, "urlopen", boom)
    import pytest
    with pytest.raises(gemini.Transient, match="no answer"):
        gemini._http_post("https://example.invalid/x", {}, "k", timeout=1)


def test_video_gemini_cannot_open_yet_is_retried_then_given_up():
    # Live mode: Signals runs the minute a stream's chat ends, before YouTube has published the
    # recording, so Gemini answers 403. That must not throw the stream's chat moments away.
    from lib.gemini import NotAccessible

    class NotYetGem(FakeGem):
        def generate_json(self, parts, prompt, schema):
            self.calls_this_run += 1
            raise NotAccessible("403 PERMISSION_DENIED: The caller does not have permission")
    fresh = {**sig("a", 100, 160, 5), "created_at": "2026-11-20T11:30:00Z"}   # found 30 min ago
    jobs = watcher.pick_jobs([cand("a", kind="live_vod", dur="0")], [fresh], RULES)
    _, wsig, wvid, notes = watcher.run(NotYetGem([]), jobs, set(), set(), RULES, 4, NOW)
    assert wvid == {"a": "watch_wait"} and wsig == {}      # windows stay pending
    assert any("retry next run" in n for n in notes)
    assert watcher.pick_jobs([cand("a", kind="live_vod", status="watch_wait")], [fresh], RULES)
    old = {**fresh, "created_at": "2026-11-19T06:00:00Z"}   # still 403 after a day: give up
    jobs = watcher.pick_jobs([cand("a", kind="live_vod", status="watch_wait")], [old], RULES)
    _, _, wvid, _ = watcher.run(NotYetGem([]), jobs, set(), set(), RULES, 4, NOW)
    assert wvid == {"a": "watch_failed"}
    jobs = watcher.pick_jobs([cand("b")], [], RULES)         # whole video, no Signals window
    _, _, wvid, _ = watcher.run(NotYetGem([]), jobs, set(), set(), RULES, 4, NOW)
    assert wvid == {"b": "watch_failed"}


def test_busy_twice_gives_up_and_failed_videos_are_skipped():
    from lib.gemini import Transient

    class BusyGem(FakeGem):
        def generate_json(self, parts, prompt, schema):
            self.calls_this_run += 1
            raise Transient("503 busy")
    jobs = watcher.pick_jobs([cand("a", status="watch_retry")], [sig("a", 100, 160, 5)], RULES)
    _, _, wvid, _ = watcher.run(BusyGem([]), jobs, set(), set(), RULES, 4, NOW)
    assert wvid == {"a": "watch_failed"}
    assert watcher.pick_jobs([cand("a", status="watch_failed")], [sig("a", 100, 160, 5)], RULES) == []
