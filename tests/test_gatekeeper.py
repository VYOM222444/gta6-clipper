import json
from datetime import datetime, timedelta, timezone

from agents import gatekeeper
from lib.config import load_rules
from tests.fakes import FakeYouTube

R = load_rules()
CFG = R["gatekeeper"]
NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)


def mom(mid, vid="v1", start=100, end=125, quality=8, reason="cop car flips off a bridge into the ocean",
        title="GTA 6 police chase", status="ready", hook="Cops flip off bridge"):
    return {"moment_id": mid, "video_id": vid, "start_s": str(start), "end_s": str(end), "source": "watcher",
            "gate_status": status, "quality": str(quality), "reason": reason, "title": title,
            "hook_idea": hook, "style_tags": "police chase, crash"}


def cand(vid="v1", name="BigStreamer", checked=""):
    return {"video_id": vid, "channel_id": "UC" + vid, "channel_title": name,
            "published_at": "2026-11-19T10:00:00Z", "sat_checked_at": checked, "sat_shorts": ""}


def test_leak_blocklist_used():
    used = {("v1", 300.0)}
    c = cand()
    assert gatekeeper.simple_gate(mom("a", title="GTA 6 LEAKED gameplay"), c, set(), {}, CFG)[0] == "rejected_leak"
    assert gatekeeper.simple_gate(mom("a", reason="dev build footage"), c, set(), {}, CFG)[0] == "rejected_leak"
    assert gatekeeper.simple_gate(mom("a", reason="a sleek car"), c, set(), {}, CFG)[0] == "ready"
    bl = {"channels": [{"name": "bigstreamer"}]}
    assert gatekeeper.simple_gate(mom("a"), c, set(), bl, CFG)[0] == "rejected_blocklist"
    assert gatekeeper.simple_gate(mom("a"), c, set(), {"channels": ["UCv1"]}, CFG)[0] == "rejected_blocklist"
    assert gatekeeper.simple_gate(mom("a", start=310), c, used, {}, CFG)[0] == "rejected_used"


def test_duplicates_keep_higher_quality():
    rows = [mom("a", start=100, end=130, quality=7), mom("b", start=110, end=135, quality=9),
            mom("c", start=400, end=420), mom("d", vid="v2", start=100, end=130)]
    drop = gatekeeper.duplicates(rows, CFG["duplicate_overlap"])
    assert list(drop) == ["a"] and "b" in drop["a"]


def test_saturation_match_needs_keywords_and_views():
    shorts = [{"id": "s1", "v": 250_000, "t": "BigStreamer cop car flips off bridge in GTA 6 💀"},
              {"id": "s2", "v": 900_000, "t": "BigStreamer buys a house in GTA 6"}]
    sat, note = gatekeeper.saturation(mom("a"), shorts, CFG["saturation"])
    assert sat and "1 Shorts" in note and "250,000" in note
    small = [{**shorts[0], "v": 3000}]
    assert gatekeeper.saturation(mom("a"), small, CFG["saturation"])[0] is False
    assert gatekeeper.saturation(mom("a"), [], CFG["saturation"]) == (False, "0 of 0 Shorts about this video show it")


def test_search_shorts_keeps_only_references():
    yt = FakeYouTube(videos=[{"id": "s1", "statistics": {"viewCount": "50000"}},
                             {"id": "s3", "statistics": {"viewCount": "10"}}])
    yt.snippet_results["BigStreamer GTA 6"] = [
        {"id": "s1", "title": "Cop flip 💀", "description": "via BigStreamer", "channelId": "x"},
        {"id": "s2", "title": "random GTA 6 short", "description": "", "channelId": "y"},
        {"id": "s3", "title": "clip", "description": "full video youtu.be/v1", "channelId": "z"},
        {"id": "v1", "title": "BigStreamer", "description": "", "channelId": "UCv1"}]
    out = gatekeeper.search_shorts(yt, cand(), CFG["saturation"])
    assert [s["id"] for s in out] == ["s1", "s3"] and out[0]["v"] == 50000


def test_run_end_to_end():
    yt = FakeYouTube(videos=[{"id": "s1", "statistics": {"viewCount": "300000"}}])
    yt.snippet_results["BigStreamer GTA 6"] = [
        {"id": "s1", "title": "BigStreamer cop car flips off the bridge", "description": "", "channelId": "x"}]
    moments = [mom("a"), mom("b", start=500, end=520, reason="buys a yacht at the marina", hook="New yacht"),
               mom("c", start=505, end=525, quality=6, reason="buys a yacht"),
               mom("old", status="in_batch"), mom("x", vid="v2", title="GTA 6 leak")]
    cands = [cand(), cand("v2", "Other", checked=(NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))]
    m_upd, c_upd, notes = gatekeeper.run(moments, cands, [], [], {}, R, yt, NOW)
    assert m_upd["a"]["gate_status"] == "rejected_saturated"
    assert m_upd["c"]["gate_status"] == "rejected_duplicate"
    assert m_upd["x"]["gate_status"] == "rejected_leak"
    assert "gate_status" not in m_upd["b"] and m_upd["b"]["saturation"].startswith("0 of 1")
    assert "old" not in m_upd
    assert list(c_upd) == ["v1"] and json.loads(c_upd["v1"]["sat_shorts"])[0]["v"] == 300000
    assert yt.used == 101                       # one search + one stats call


def test_no_saturation_search_for_moments_too_old_for_the_decider():
    # Searches have their own YouTube limit (100/day). Moments older than the Decider's window are
    # never picked, so re-checking their source videos every 12 h only starves Scout's searches.
    yt = FakeYouTube()
    stamp = lambda h: (NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = {**mom("a"), "watched_at": stamp(200)}
    fresh = {**mom("b", vid="v2", reason="buys a yacht at the marina", hook="New yacht"),
             "watched_at": stamp(3)}
    undated = mom("c", vid="v3", reason="jumps a train with a bike", hook="Train jump")
    cands = [cand(), cand("v2", "Other"), cand("v3", "Third")]
    _, c_upd, _ = gatekeeper.run([stale, fresh, undated], cands, [], [], {}, R, yt, NOW, max_age_hours=48)
    assert sorted(c_upd) == ["v2", "v3"]       # the 200 h old moment's video is not searched
    assert ("snippets", "BigStreamer GTA 6") not in yt.calls
    _, c_upd, _ = gatekeeper.run([stale], [cand()], [], [], {}, R, FakeYouTube(), NOW)
    assert list(c_upd) == ["v1"]               # no age limit given: old behaviour


def test_run_without_youtube_key_still_gates():
    m_upd, c_upd, _ = gatekeeper.run([mom("a"), mom("b", start=105)], [cand()], [], [], {}, R, None, NOW)
    assert m_upd == {"b": {"gate_status": "rejected_duplicate",
                           "gate_note": "same moment as a (higher quality)"}} or "a" in m_upd
    assert c_upd == {}
