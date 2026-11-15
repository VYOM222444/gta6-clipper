import json

import pytest

from lib.youtube import QuotaExceeded, YouTube, YouTubeError, chunks


def fake_get(responses):
    calls = []

    def get(url):
        calls.append(url)
        return responses.pop(0)
    return get, calls


def test_missing_key_is_clear():
    with pytest.raises(YouTubeError, match="YOUTUBE_API_KEY"):
        YouTube(api_key="")


def test_search_counts_100_units_and_parses_ids():
    get, calls = fake_get([{"items": [{"id": {"videoId": "a"}}, {"id": {"kind": "x"}}]}])
    yt = YouTube(api_key="k", budget_units=1000, http_get=get)
    assert yt.search_video_ids("GTA 6", "2026-11-14T00:00:00Z", "hi", "completed") == ["a"]
    assert yt.used == 100
    assert "relevanceLanguage=hi" in calls[0] and "eventType=completed" in calls[0]


def test_budget_stops_before_call():
    get, calls = fake_get([])
    yt = YouTube(api_key="k", budget_units=150, http_get=get)
    yt.used = 100
    with pytest.raises(QuotaExceeded):
        yt.search_video_ids("x", "2026-01-01T00:00:00Z")
    assert calls == []


def test_videos_batches_of_50_cost_1_each():
    get, calls = fake_get([{"items": []}, {"items": []}, {"items": []}])
    yt = YouTube(api_key="k", budget_units=100, http_get=get)
    yt.videos([f"v{i}" for i in range(120)])
    assert yt.used == 3 and len(calls) == 3


def test_error_hides_key():
    def boom(url):
        raise YouTubeError("403 quotaExceeded: The request cannot be completed")
    yt = YouTube(api_key="SECRET123", budget_units=500, http_get=boom)
    with pytest.raises(YouTubeError) as e:
        yt.channels(["UCx"])
    assert "SECRET123" not in str(e.value)
    assert "channels" in str(e.value)


def test_recent_uploads_uses_uploads_playlist():
    get, calls = fake_get([{"items": [{"contentDetails": {"videoId": "z"}}]}])
    yt = YouTube(api_key="k", budget_units=10, http_get=get)
    assert yt.recent_uploads("UCabc") == ["z"]
    assert "playlistId=UUabc" in calls[0]
    assert yt.recent_uploads("bad") == []


def test_chunks():
    assert [len(c) for c in chunks(range(101), 50)] == [50, 50, 1]
