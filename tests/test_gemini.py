import json
from datetime import datetime, timezone

import pytest

from lib import gemini


def usage(tmp_path, when=datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)):
    return gemini.Usage(tmp_path / "u.json", now=when)


def ok_answer(obj):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}}]}


def test_quota_day_is_pacific():
    # 06:00 UTC on Nov 20 is still Nov 19 in California
    assert gemini.quota_day(datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)) == "2026-11-19"
    assert gemini.quota_day(datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)) == "2026-11-20"


def test_usage_persists_and_trims(tmp_path):
    u = usage(tmp_path)
    u.add("watcher", 2)
    assert gemini.Usage(tmp_path / "u.json", now=datetime(2026, 11, 20, 6, tzinfo=timezone.utc)).used("watcher") == 2
    data = {f"2026-10-{d:02d}": {"watcher": 1} for d in range(1, 21)}
    (tmp_path / "u.json").write_text(json.dumps(data))
    u2 = usage(tmp_path)
    u2.add("watcher")
    assert len(json.loads((tmp_path / "u.json").read_text())) == 14


def test_video_parts_windows():
    assert gemini.video_parts("U") == [{"fileData": {"fileUri": "U"}}]
    p = gemini.video_parts("U", [(10.4, 70)])
    assert p == [{"fileData": {"fileUri": "U"}, "videoMetadata": {"startOffset": "10s", "endOffset": "70s"}}]


def test_extract_json_handles_fences_and_blocks():
    assert gemini.extract_json(ok_answer({"a": 1})) == {"a": 1}
    fenced = {"candidates": [{"content": {"parts": [{"text": "```json\n{\"a\": 2}\n```"}]}}]}
    assert gemini.extract_json(fenced) == {"a": 2}
    with pytest.raises(gemini.GeminiError, match="SAFETY"):
        gemini.extract_json({"candidates": [{"finishReason": "SAFETY"}]})


def test_budget_counts_and_stops(tmp_path):
    calls = []

    def post(url, body, key):
        calls.append((url, body))
        return ok_answer({"moments": []})
    g = gemini.Gemini(["m1"], 2, "watcher", api_key="K", usage=usage(tmp_path), http_post=post)
    g.generate_json([], "p", {})
    g.generate_json([], "p", {})
    with pytest.raises(gemini.BudgetReached):
        g.generate_json([], "p", {})
    assert len(calls) == 2 and g.usage.used("watcher") == 2
    assert "m1:generateContent" in calls[0][0]
    assert calls[0][1]["generationConfig"]["responseMimeType"] == "application/json"


def test_unknown_model_falls_through(tmp_path):
    def post(url, body, key):
        if "old-model" in url:
            raise gemini.ModelNotFound("404")
        return ok_answer({"ok": True})
    g = gemini.Gemini(["old-model", "new-model"], 5, "watcher", api_key="K", usage=usage(tmp_path), http_post=post)
    answer, model = g.generate_json([], "p", {})
    assert answer == {"ok": True} and model == "new-model"
    assert g.models == ["new-model"]


def test_missing_key():
    with pytest.raises(gemini.GeminiError, match="GEMINI_API_KEY"):
        gemini.Gemini(["m"], 1, "w", api_key="")


def test_busy_retries_once_and_counts(tmp_path):
    attempts = []

    def post(url, body, key):
        attempts.append(1)
        if len(attempts) == 1:
            raise gemini.Transient("503 UNAVAILABLE")
        return ok_answer({"ok": 1})
    waits = []
    g = gemini.Gemini(["m"], 5, "watcher", api_key="K", usage=usage(tmp_path), http_post=post,
                      retry_wait=7, sleep=waits.append)
    assert g.generate_json([], "p", {})[0] == {"ok": 1}
    assert waits == [7] and g.usage.used("watcher") == 2


def test_busy_twice_raises(tmp_path):
    def post(url, body, key):
        raise gemini.Transient("503")
    g = gemini.Gemini(["m"], 5, "watcher", api_key="K", usage=usage(tmp_path), http_post=post,
                      retry_wait=0, sleep=lambda s: None)
    with pytest.raises(gemini.Transient):
        g.generate_json([], "p", {})
