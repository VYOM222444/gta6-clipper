import json
from datetime import datetime, timezone

from agents import signals
from lib import sheets
from lib.config import load_rules
from lib.schema import TABS
from tests.fakes import FakeWorksheet

RULES = load_rules()
CFG = RULES["signals"]
NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)


def chat_line(ms, *runs):
    return json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": str(ms), "actions": [
        {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {"message": {"runs": list(runs)}}}}}
    ]}})


def test_parse_chat_line_text_and_emoji():
    line = chat_line(65400, {"text": "NO WAY "}, {"emoji": {"emojiId": "💀", "shortcuts": [":skull:"]}})
    assert signals.parse_chat_line(line) == [(65.4, "NO WAY  💀")]
    custom = chat_line(1000, {"emoji": {"emojiId": "UCx/abc", "shortcuts": [":speedLaugh:"], "isCustomEmoji": True}})
    assert signals.parse_chat_line(custom) == [(1.0, ":speedLaugh:")]
    assert signals.parse_chat_line("not json") == []
    assert signals.parse_chat_line(json.dumps({"other": 1})) == []


def test_reaction_matching_uses_word_boundaries():
    m = signals._reaction_matcher(CFG["reaction_words"])
    assert m("LMAOOOO") and m("LMAO") and m("www") and m("brooo")
    assert m("lol that was crazy")
    assert m("CLIP IT")
    assert m("💀💀")
    assert m("w")
    assert not m("hello world")      # 'w' inside a word doesn't count
    assert not m("lollipop")


def test_chat_spike_found_with_lead_time():
    msgs = []
    for t in range(0, 600, 5):            # quiet chat: 2 messages / 10 s, 1 reaction
        msgs += [(t, "hi"), (t + 0.5, "lol") if t % 10 == 0 else (t + 0.5, "ok")]
    msgs += [(300 + i * 0.2, "LMAO 💀") for i in range(40)]   # big moment at 300-308 s
    spikes = signals.chat_spikes(msgs, CFG)
    assert len(spikes) == 1
    s = spikes[0]
    assert s["start"] == 300 - CFG["chat_lead_seconds"] and s["end"] == 310 + CFG["chat_tail_seconds"]
    assert s["strength"] > 10


def test_no_spike_in_flat_chat():
    msgs = [(t, "lol") for t in range(0, 600, 2)]
    assert signals.chat_spikes(msgs, CFG) == []
    assert signals.chat_spikes([], CFG) == []


def test_replay_peaks_skip_intro_and_need_local_max():
    heat = [{"start_time": i * 60, "end_time": (i + 1) * 60, "value": 0.2} for i in range(100)]
    heat[0]["value"] = 1.0          # intro replays: ignored (first 5%)
    heat[40]["value"] = 0.9
    heat[41]["value"] = 0.7          # shoulder of the same peak
    peaks = signals.replay_peaks(heat, 6000, CFG)
    assert [p["start"] for p in peaks] == [2400 - CFG["replay_pad_seconds"]]


def test_comment_timestamps():
    assert signals.comment_timestamps("4:32 💀 and 1:02:03 lol", 10_000) == [272, 3723]
    assert signals.comment_timestamps("at 12:75 or 99:00", 1000) == []
    assert signals.comment_timestamps("score 2:1", 1000) == []


def test_comment_peaks_need_two_mentions():
    comments = [("4:32 💀", 10), ("4:35 bro", 0), ("10:00 nice", 3)]
    peaks = signals.comment_peaks(comments, 3600, CFG)
    assert len(peaks) == 1 and peaks[0]["start"] == 272 - CFG["comments_before_seconds"]
    assert "2 comments" in peaks[0]["note"]


def test_merge_combines_signals_and_ranks():
    w = [
        {"start": 270, "end": 320, "signal": "chat", "strength": 9, "note": "chat"},
        {"start": 290, "end": 330, "signal": "comments", "strength": 3, "note": "cm"},
        {"start": 2000, "end": 2050, "signal": "chat", "strength": 4, "note": "chat2"},
    ]
    merged = signals.merge_windows(w, CFG)
    assert len(merged) == 2
    top = merged[0]
    assert top["signals"] == {"chat", "comments"} and top["start"] == 270 and top["end"] == 330


def test_merge_respects_max_length():
    w = [{"start": i * 40, "end": i * 40 + 50, "signal": "chat", "strength": 6, "note": ""} for i in range(5)]
    for m in signals.merge_windows(w, CFG):
        assert m["end"] - m["start"] <= CFG["max_window_seconds"]


def test_to_moments_rows():
    merged = [{"start": 270.4, "end": 330, "signals": {"comments", "chat"}, "notes": ["a", "b"], "points": 4.5}]
    row = signals.to_moments("vid1", merged, NOW)[0]
    assert row["moment_id"] == "vid1-270" and row["signal"] == "chat+comments"
    assert row["source"] == "signal" and row["gate_status"] == "pending" and row["reason"] == "a; b"


def test_pick_videos_filters():
    cands = [
        {"video_id": "a", "kind": "live_vod", "score": "10", "title": "GTA 6 launch stream", "status": "new", "duration_s": "20000"},
        {"video_id": "b", "kind": "short", "score": "99", "title": "GTA 6 short", "status": "new", "duration_s": "30"},
        {"video_id": "c", "kind": "long", "score": "50", "title": "GTA 5 LIVE #gta6", "status": "new", "duration_s": "900"},
        {"video_id": "d", "kind": "long", "score": "40", "title": "GTA 6 cops", "status": "signals_done", "duration_s": "900"},
        {"video_id": "e", "kind": "long", "score": "30", "title": "GTA 6 review", "status": "new", "duration_s": "900"},
        {"video_id": "f", "kind": "long", "score": "20", "title": "GTA 6 tiny", "status": "new", "duration_s": "120"},
        {"video_id": "g", "kind": "long", "score": "60", "title": "GTA 6 fading", "status": "new", "duration_s": "900"},
    ]
    trk = [{"video_id": "g", "label": "Fading"}]
    assert [c["video_id"] for c in signals.pick_videos(cands, trk, RULES)] == ["e", "a"]
    cands[4]["status"] = "comments_done"
    assert [c["video_id"] for c in signals.pick_videos(cands, trk, RULES, mode="api")] == ["a"]
    assert [c["video_id"] for c in signals.pick_videos(cands, trk, RULES, mode="full")] == ["e", "a"]


def test_blocked_detection():
    assert signals.is_blocked("ERROR: [youtube] abc: Sign in to confirm you're not a bot")
    assert not signals.is_blocked("ERROR: video unavailable")


def test_set_column_updates_status_only():
    headers = TABS["Candidates"]
    ws = FakeWorksheet("Candidates", [headers])
    sheets.upsert_rows(ws, headers, "video_id", [{"video_id": "a", "status": "new", "title": "T"},
                                                  {"video_id": "b", "status": "new"}])
    assert sheets.set_column(ws, headers, "video_id", "status", {"a": "signals_done"}) == 1
    rows = ws.get_all_records()
    assert rows[0]["status"] == "signals_done" and rows[0]["title"] == "T"
    assert rows[1]["status"] == "new"


def test_fetch_ytdlp_reads_partial_files(tmp_path, monkeypatch):
    (tmp_path / "vid.info.json").write_text(json.dumps({"duration": 600, "heatmap": []}))
    (tmp_path / "vid.live_chat.json.part").write_text(chat_line(5000, {"text": "lol"}) + "\n")

    class P:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(signals.subprocess, "run", lambda *a, **k: P())
    info, msgs, err = signals.fetch_ytdlp("vid", True, str(tmp_path), 10)
    assert info["duration"] == 600 and msgs == [(5.0, "lol")] and err == ""


def test_set_values_many_columns_one_write():
    headers = TABS["Batch"]
    ws = FakeWorksheet("Batch", [headers])
    sheets.upsert_rows(ws, headers, "batch_id", [{"batch_id": "b1", "approved": "yes"}, {"batch_id": "b2"}])
    n = sheets.set_values(ws, headers, "batch_id", {"b1": {"render_status": "done", "output_path": "/x.mp4"}})
    rows = ws.get_all_records()
    assert n == 2 and rows[0]["render_status"] == "done" and rows[0]["output_path"] == "/x.mp4"
    assert rows[0]["approved"] == "yes" and rows[1]["render_status"] == ""


def test_api_mode_rechecks_young_videos_after_new_ones():
    def c(vid, status, age_h, checked_h=None, score="5"):
        return {"video_id": vid, "kind": "long", "duration_s": "3600", "score": score,
                "title": "GTA 6 chaos stream", "status": status,
                "published_at": (NOW - __import__("datetime").timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "signals_at": "" if checked_h is None else
                (NOW - __import__("datetime").timedelta(hours=checked_h)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    cands = [c("old", "comments_done", 60, 10, score="99"),     # too old: no re-check
             c("recent", "watched", 20, 2, score="98"),          # checked 2 h ago: wait
             c("due", "watched", 20, 7, score="97"),             # due again
             c("new", "new", 3, score="1")]                      # never checked: first
    got = [x["video_id"] for x in signals.pick_videos(cands, [], RULES, "api", NOW)]
    assert got == ["new", "due"]
    full = [x["video_id"] for x in signals.pick_videos(cands, [], RULES, "full", NOW)]
    assert "due" in full and "new" in full


def test_drop_known_windows():
    merged = [{"start": 100, "end": 140}, {"start": 400, "end": 440}]
    known = [{"video_id": "v", "start_s": "95", "end_s": "150"}, {"video_id": "x", "start_s": "400", "end_s": "450"}]
    assert signals.drop_known("v", merged, known) == [{"start": 400, "end": 440}]


def test_loudness_peaks_finds_bursts():
    from agents.signals import loudness_peaks, merge_windows
    from lib.config import load_rules
    cfg = load_rules()["signals"]
    db = [-40.0] * 300
    for t in (120, 121, 122):
        db[t] = -18.0
    db[240] = -20.0
    db[5] = 0.0  # inside the skipped intro (first 5%)
    w = loudness_peaks(db, cfg)
    assert [x["start"] for x in sorted(w, key=lambda x: x["start"])] == [100, 220]
    assert all(x["signal"] == "loud" for x in w)
    assert merge_windows(w, cfg)  # 'loud' has points
    assert loudness_peaks([-40.0] * 30, cfg) == []
def test_apps_script_reaction_words_match_rules():
    import json
    import re
    from lib.config import REPO_ROOT, load_rules
    gs = (REPO_ROOT / "tools" / "apps_script_trigger.gs").read_text(encoding="utf-8")
    words = json.loads(re.search(r"var REACTION_WORDS = (\[.*?\]);", gs).group(1))
    assert words == load_rules()["signals"]["reaction_words"]


def test_live_chat_counts_make_spikes():
    from agents.signals import analyse, live_chat_counts, pick_videos
    from lib.config import load_rules
    rules = load_rules()
    cfg = rules["signals"]
    rows = [{"video_id": "V1", "bin_s": b * 10, "reactions": 2, "messages": 20} for b in range(60)]
    rows.append({"video_id": "V1", "bin_s": 300, "reactions": 30, "messages": 80})  # same bin, adds up
    counts = live_chat_counts(rows, cfg)
    assert counts["V1"][30] == 32
    merged = analyse({"duration_s": 600}, None, [], [], cfg, live_counts=counts["V1"])
    assert merged and merged[0]["start"] == 300 - cfg["chat_lead_seconds"] and "chat" in merged[0]["signals"]
    cand = [{"video_id": "V1", "kind": "live_vod", "duration_s": 7200, "title": "GTA 6 live stream", "score": 1,
             "status": "new"}]
    assert pick_videos(cand, [], rules, "api", live=frozenset({"V1"}))


def test_main_api_mode_runs_end_to_end(monkeypatch):
    """Regression: a loop variable named `live` once shadowed the LiveChat dict and crashed main()."""
    from datetime import datetime, timedelta, timezone
    from agents import signals
    from lib import sheets
    from lib.schema import TABS
    from tests.fakes import FakeSpreadsheet, FakeWorksheet

    def tab(name, rows):
        head = TABS[name]
        return FakeWorksheet(name, [head] + [[str(r.get(h, "")) for h in head] for r in rows])

    pub = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cand = [{"video_id": "VODVIDEO001", "title": "GTA 6 live stream chaos", "kind": "live_vod",
             "duration_s": "7200", "status": "new", "score": "10", "published_at": pub, "views": "50000"},
            {"video_id": "LONGVIDEO02", "title": "GTA 6 gameplay part 1", "kind": "long",
             "duration_s": "1800", "status": "new", "score": "9", "published_at": pub, "views": "40000"}]
    live = [{"key": f"VODVIDEO001-{b * 10}", "video_id": "VODVIDEO001", "bin_s": b * 10,
             "reactions": 40 if b == 300 else 2, "messages": 50} for b in range(600)]
    ss = FakeSpreadsheet([tab("Candidates", cand), tab("Tracker", []), tab("Moments", []),
                          tab("LiveChat", live)])

    class YT:
        used, budget = 0, 100

        def __init__(self, budget_units=None):
            pass

        def top_comments(self, vid, n):
            return []

    monkeypatch.setenv("SIGNALS_MODE", "api")
    monkeypatch.setattr(sheets, "get_client", lambda: None)
    monkeypatch.setattr(sheets, "open_sheet", lambda c, i: ss)
    monkeypatch.setattr(signals, "YouTube", YT)
    assert signals.main() == 0
    moments = sheets.read_records(ss.worksheet("Moments"))
    assert any(m["video_id"] == "VODVIDEO001" and "chat" in m["signal"] for m in moments)
