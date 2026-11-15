from datetime import datetime, timedelta, timezone

from agents import tracker
from lib.config import load_rules
from tests.fakes import FakeYouTube

CFG = load_rules()["tracker"]
PUB = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)


def snaps(*views, start_h=6, step_h=6):
    return [{"t": PUB + timedelta(hours=start_h + i * step_h), "views": v, "likes": 0,
             "comments": 0} for i, v in enumerate(views)]


def label(s, subs=None):
    now = s[-1]["t"]
    return tracker.compute(s, PUB, subs, now, CFG)


def test_new_when_young():
    assert label(snaps(100, start_h=2))["label"] == "New"


def test_velocities():
    v = tracker.velocities(snaps(600, 1200, 3000), PUB)
    assert v == [100.0, 100.0, 300.0]


def test_rising_and_breakout():
    r = label(snaps(600, 1200, 3000), subs=10_000)
    assert r["label"] == "Rising" and r["acceleration"] == 3.0
    b = label(snaps(600, 1200, 3000), subs=500)
    assert b["label"] == "Breakout"


def test_steady():
    assert label(snaps(600, 1200, 1800))["label"] == "Steady"


def test_fading_then_stopped():
    r = label(snaps(6000, 9000, 10000, 10300))       # velocities 1000, 500, 166, 50
    assert r["label"] == "Fading" and r["tracking"] == "stopped"
    r2 = label(snaps(6000, 9000, 10000))             # two slowdowns
    assert r2["label"] == "Fading" and r2["tracking"] == "active"


def test_mature_stops():
    s = snaps(100, 200, start_h=7 * 24, step_h=6)
    r = label(s)
    assert r["label"] == "Mature" and r["tracking"] == "stopped"


def test_engagement_and_reach():
    s = [{"t": PUB + timedelta(hours=10), "views": 1000, "likes": 40, "comments": 10}]
    r = tracker.compute(s, PUB, 500.0, s[-1]["t"], CFG)
    assert r["engagement"] == 0.05 and r["reach_score"] == 2.0 and r["acceleration"] == ""


def test_plan_skips_blocked_stopped_and_duplicates():
    cands = [{"video_id": "a", "status": "new"}, {"video_id": "a", "status": "new"},
             {"video_id": "b", "status": "blocked_leak"}, {"video_id": "c", "status": "new"},
             {"video_id": "", "status": "new"}]
    trk = [{"video_id": "c", "tracking": "stopped"}]
    assert [c["video_id"] for c in tracker.plan(cands, trk)] == ["a"]


def vid(i, views, live=None):
    v = {"id": i, "statistics": {"viewCount": str(views), "likeCount": "5", "commentCount": "1"}}
    if live:
        v["liveStreamingDetails"] = live
    return v


def test_run_snapshots_gone_and_gap(tmp_path):
    now = PUB + timedelta(hours=12)
    cands = [
        {"video_id": "a", "title": "A", "published_at": "2026-11-20T00:00:00Z", "subscribers": "100", "kind": "long"},
        {"video_id": "gone", "title": "G", "published_at": "2026-11-20T00:00:00Z", "subscribers": ""},
        {"video_id": "l", "title": "L", "published_at": "2026-11-20T00:00:00Z", "subscribers": "", "kind": "live_now"},
    ]
    history = {"l": [{"t": now - timedelta(minutes=10), "views": 50, "likes": 0, "comments": 0}]}
    yt = FakeYouTube(videos=[vid("a", 1200), vid("l", 80, live={"actualEndTime": "x"})])
    recs, new = tracker.run(yt, cands, [], history, CFG, now)
    by = {r["video_id"]: r for r in recs}
    assert by["a"]["velocity"] == 100.0 and by["a"]["snapshots"] == 1
    assert by["gone"]["tracking"] == "gone"
    assert by["l"]["kind"] == "live_vod" and by["l"]["snapshots"] == 1   # too soon: no new snapshot
    assert by["l"]["views"] == 80
    assert [n["video_id"] for n in new] == ["a"]

    path = tmp_path / "snaps.csv"
    tracker.append_snapshots(new, path)
    tracker.append_snapshots(new, path)
    h = tracker.load_history(path)
    assert len(h["a"]) == 2 and h["a"][0]["views"] == 1200
    assert path.read_text().count("snapshot_at") == 1
