import numpy as np

from lib import edits

CFG = {"tail_seconds": 1.5, "min_seconds": 8, "max_zooms": 4, "replay_before": 1.2, "replay_after": 0.8,
       "replay_speed": 0.5}
ALL = {"zoom": True, "impact": True, "whoosh": True, "replay": True, "end_after_payoff": True}


def tone(sec, loud_at=()):
    t = np.arange(int(sec * 16000)) / 16000
    a = 0.03 * np.sin(2 * np.pi * 200 * t)
    for s, e, v in loud_at:
        a[(t >= s) & (t < e)] += v * np.sin(2 * np.pi * 90 * t[(t >= s) & (t < e)])
    return a.astype("float32")


def test_analyze_finds_payoff_and_hits():
    payoff, hits = edits.analyze(tone(14, [(4, 4.3, 0.5), (9, 9.6, 0.9)]))
    assert 9 <= payoff <= 9.6 and hits == [4.0, 9.0]
    assert edits.analyze(np.zeros(16000 * 5, dtype="float32")) == (None, [])


def test_plan_all_effects():
    p = edits.plan(14.0, 9.25, [4.0, 9.0], ALL, CFG)
    assert p["end"] == 10.75 and p["zooms"] == [4.0, 9.0] and p["replay"] == (8.05, 10.05)
    assert p["impacts"] == [9.25, 13.15]           # payoff + payoff again inside the slow replay
    assert 10.45 in p["whooshes"]                   # into the replay


def test_plan_nothing_on():
    p = edits.plan(14.0, 9.25, [4.0], {}, CFG)
    assert p == {"end": 14.0, "zooms": [], "replay": None, "impacts": [], "whooshes": [], "payoff": 9.25,
                 "shake": None, "freeze": None, "loop": False}
    assert edits.pass2_cmd("a", "b", p, "i", "w", CFG) is None


def test_flags_now_beats_7day_beats_default():
    rows = [{"rule_id": "research.sound.sfx_impact", "rule": "DO: sfx impact (winners 60% vs average 20%)"},
            {"rule_id": "research.now.sound.sfx_impact", "rule": "AVOID: sfx impact (winners 5% vs average 40%)"},
            {"rule_id": "research.editing.zoom_ins", "rule": "info: zoom ins (median: winners 3 vs average 0)"},
            {"rule_id": "research.editing.slow_motion", "rule": "DO: slow motion (winners 50% vs average 10%)"}]
    flags, src = edits.flags_from_playbook(rows, {"whoosh": True, "impact": True})
    assert flags["impact"] is False and src["impact"] == "now"
    assert flags["zoom"] is True and src["zoom"] == "7-day"
    assert flags["replay"] is True and flags["whoosh"] is True and src["whoosh"] == "default"


def test_zoom_chain_uses_only_basic_filters():
    assert edits.zoom_chain("a", "b", [], 1080, 1920) == ""
    c = edits.zoom_chain("g0", "g", [4.0, 9.0], 1080, 1920)
    assert "between(t,4.00,4.70)+between(t,9.00,9.70)" in c and "overlay=0:0:enable=" in c
    assert "eval=frame" not in c and "scale=1210:2150" in c   # no time variable inside scale


def test_pass2():
    p = edits.plan(14.0, 9.25, [4.0], ALL, CFG)
    cmd = " ".join(edits.pass2_cmd("m.mp4", "o.mp4", p, "i.wav", "w.wav", CFG))
    assert "atempo=0.5" in cmd and "concat=n=2" in cmd and "adelay=9250|9250" in cmd


def test_shake_and_freeze_plan_and_cmd():
    p = edits.plan(14.0, 9.25, [4.0], {"shake": True, "freeze": True, "whoosh": True, "zoom": True,
                                       "impact": True}, CFG)
    # punch freeze (default): frozen zoomed frame over 9.25-9.85 s, sound plays on, shake right after
    assert p["freeze"] == (9.25, 0.6) and p["shake"] == (9.85, 10.25)
    assert p["impacts"] == [9.25]                       # impact lands as the frame freezes, no shift
    cmd = " ".join(edits.pass2_cmd("m.mp4", "o.mp4", p, "i.wav", "w.wav", CFG))
    assert "tpad=stop_mode=clone:stop_duration=0.6" in cmd and "setpts=PTS+9.25/TB" in cmd
    assert "color=white@0.55" in cmd and "anullsrc" not in cmd    # flash, and no silent gap
    assert "libx264" in cmd                              # re-encoded, not copied
    assert {"shake", "freeze"} <= set(edits.tags(p))
    # old style "hold": whole frame held with silence, everything after plays later
    hold = edits.plan(14.0, 9.25, [4.0], {"shake": True, "freeze": True, "impact": True},
                      {**CFG, "freeze_style": "hold"})
    assert hold["shake"] == (9.25, 9.65) and "anullsrc" in " ".join(
        edits.pass2_cmd("m.mp4", "o.mp4", hold, "i.wav", "w.wav", {**CFG, "freeze_style": "hold"}))


def test_experiment_flags_are_stable_and_about_half():
    on = [edits.experiment_flags({}, f"2026-11-25-{i:02d}", {"shake": 0.5})["shake"] for i in range(200)]
    assert 70 < sum(on) < 130
    assert edits.experiment_flags({}, "x-1", {"shake": 0.5}) == edits.experiment_flags({}, "x-1", {"shake": 0.5})
    assert edits.experiment_flags({}, "x-1", {"shake": 0})["shake"] is False


def test_loop_ending_plan_and_command():
    from lib import edits
    cfg = {"tail_seconds": 1.5, "min_seconds": 8}
    p = edits.plan(20, 12.0, [], {"loop": True}, cfg)
    assert p["loop"] and "loop" in edits.tags(p)
    p2 = edits.plan(20, 12.0, [], {"loop": True, "replay": True}, cfg)
    assert p2["replay"] and not p2["loop"]          # never loop into a slow-mo replay
    assert not edits.plan(3, 1.0, [], {"loop": True}, cfg)["loop"]  # too short
    cmd = edits.loop_cmd("in.mp4", "out.mp4", 0.2, 7.3, True)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert cmd.count("-i") == 1 and "trim=start=0.2:end=7.3" in fc     # hard cut, no cross-fade
    assert "xfade" not in fc and "afade=t=out:st=7.07:d=0.03" in fc and "[a]" in cmd
    silent = edits.loop_cmd("in.mp4", "out.mp4", 0.0, 6.0, False)
    assert "[a]" not in silent and "afade" not in silent[silent.index("-filter_complex") + 1]


def test_loop_points_pick_the_end_frame_that_looks_like_the_start():
    import numpy as np
    from lib import edits
    rng = np.random.default_rng(1)
    frames = rng.uniform(0, 255, (100, 8, 6)).astype(np.float32)    # 10 s at 10 fps, all different
    frames[95] = frames[2] + 1                                       # 9.5 s looks like 0.2 s
    start, end, jump = edits.loop_points(frames, 10, 7.5, 9.8, max_start=0.5)
    assert (start, end) == (0.2, 9.6) and jump == 1.0
    frames[60] = frames[0]                                           # perfect match, but before the payoff
    assert edits.loop_points(frames, 10, 7.5, 9.8)[1] >= 7.5
    frames[99] = frames[0]                                           # perfect match, but too long after it
    assert edits.loop_points(frames, 10, 7.5, 9.5)[1] <= 9.5
    assert edits.loop_points(frames[:30], 10, 1.0, 3.0) is None      # 3 s: too short to loop
    assert edits.loop_window({"payoff": 5.0, "freeze": (5.0, 0.6)}, 20) == (5.9, 7.9)
    assert edits.loop_window({"payoff": 5.0, "freeze": None}, 6.0) == (5.3, 6.0)
    assert edits.loop_window({"payoff": None}, 20) == (18.0, 20.0)


# --- dead-air cutter + boom / ding (28 Nov) ----------------------------------------------------

DA = {"deadair_drop_db": 20, "deadair_min_gap": 0.6, "deadair_pad": 0.15, "deadair_protect": 2.0,
      "deadair_max_share": 0.3, "min_seconds": 8}


def rms_track(sec, quiet=()):
    """Loudness frames (0.05 s): talking level 0.3, quiet stretches at 0.003 (-40 dB lower)."""
    n = int(round(sec / edits.FRAME))
    r = np.full(n, 0.3)
    for a, b in quiet:
        r[int(round(a / edits.FRAME)):int(round(b / edits.FRAME))] = 0.003
    return r


def test_dead_air_cuts_pauses_but_keeps_the_build_up_and_the_end():
    r = rms_track(30, [(0, 1.0), (5, 7), (16.5, 19), (28.5, 30)])   # payoff at 20 s
    keep = edits.dead_air(r, 20.0, DA)
    # leading 1 s cut from 0; 5-7 s cut with 0.15 s kept each side; 16.5-19 overlaps the 2 s build-up
    # before the payoff (18-20.5): only 16.65-18 goes; the pause at the very end stays
    assert keep == [(0.85, 5.15), (6.85, 16.65), (18.0, 30.0)]
    assert edits.dead_air(rms_track(30), 20.0, DA) == []              # no pauses: nothing to cut
    assert edits.dead_air(rms_track(30, [(5, 5.4)]), None, DA) == []  # 0.4 s pause: too short


def test_dead_air_never_removes_too_much():
    r = rms_track(12, [(1, 4), (5, 8.5)])                 # 6.5 s of pauses in a 12 s clip
    keep = edits.dead_air(r, None, DA)
    removed = 12 - sum(b - a for a, b in keep)
    assert removed <= 0.3 * 12 + 1e-6 and sum(b - a for a, b in keep) >= 8
    assert keep == [(0.0, 5.15), (8.35, 12.0)]           # the longest pause that fits the 30% cap


def test_deadair_cmd_uses_basic_filters_and_fades_every_part():
    cmd = edits.deadair_cmd("s.mp4", "t.mp4", [(0.85, 5.15), (6.85, 16.65)])
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "split=2" in fc and "asplit=2" in fc and "concat=n=2:v=1:a=1[v][a]" in fc
    assert "trim=start=0.85:end=5.15" in fc and "afade=t=out:st=4.285" in fc
    one = edits.deadair_cmd("s.mp4", "t.mp4", [(1.0, 9.0)], has_audio=False)
    fc1 = one[one.index("-filter_complex") + 1]
    assert "split" not in fc1 and "[0:v]trim=start=1.0:end=9.0" in fc1 and "-an" not in one


def test_payoff_sound_picks_boom_for_fails_and_ding_for_reveals():
    assert edits.payoff_sound({"content_type": "tip", "title": "GTA 6 hidden money trick"}) == "ding"
    assert edits.payoff_sound({"content_type": "secret"}) == "ding"
    assert edits.payoff_sound({"content_type": "chaos", "hook_text": "Worst driver ever 💀"}) == "boom"
    assert edits.payoff_sound({"content_type": "chaos", "brand": "busted_again", "title": "GTA 6 crash"}) == "boom"
    assert edits.payoff_sound({"content_type": "chaos", "title": "GTA 6 police chase"}) is None   # impact stays


def test_boom_replaces_the_impact_and_shows_in_the_tags():
    p = edits.plan(14.0, 9.25, [4.0], {"impact": True}, CFG)
    edits.apply_payoff_sound(p, "boom", {"boom_volume": 0.8})
    assert p["impacts"] == [9.25] and p["payoff_sfx"] == "boom"
    cmd = " ".join(edits.pass2_cmd("m.mp4", "o.mp4", p, "boom.wav", "w.wav", CFG))
    assert "boom.wav" in cmd and "volume=0.8" in cmd
    q = edits.apply_payoff_sound(edits.plan(14.0, 9.25, [], {}, CFG), "ding", {})   # impact off: still one sound
    assert q["impacts"] == [9.25] and q["impact_volume"] == 0.6
    q["deadair"] = (3, 4.2)
    assert "ding" in edits.tags(q) and "deadair3" in edits.tags(q)
    assert edits.apply_payoff_sound(edits.plan(14.0, None, [], {}, CFG), "boom", {}).get("payoff_sfx") is None
    assert len(edits.meme_sfx_cmds("b.wav", "d.wav")) == 2
