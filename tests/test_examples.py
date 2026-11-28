import json

from lib import examples


def e(i, breakout, **f):
    return ({"url": f"https://youtube.com/shorts/v{i}", "title": f"t{i}", "breakout": str(breakout),
             "views": "1000", "channel_title": "c"}, f)


def test_viral_tricks_ranked_by_lift_and_need_real_use():
    win = [{"screen_shake": True, "emoji_or_stickers": True}] * 6 + [{"screen_shake": True}] * 4
    avg = [{"screen_shake": True}] * 3 + [{}] * 7
    hot = examples.viral_tricks(win, avg)
    assert [t["key"] for t in hot[:2]] == ["screen_shake", "emoji_or_stickers"]   # +70 vs +60 points
    assert all(t["w"] >= 0.15 and t["lift"] > 0 for t in hot)


def test_ten_examples_per_trick_best_breakouts_first_fixed_slots():
    entries = [e(i, i, screen_shake=True) for i in range(1, 13)] + [e(99, 50, emoji_or_stickers=True)]
    hot = examples.viral_tricks([x[1] for x in entries], [{}] * 10)
    recs = {r["example_id"]: r for r in examples.build(entries, hot, "now", tricks=2, per=10, top=3)}
    t1 = "trick1" if hot[0]["key"] == "screen_shake" else "trick2"
    assert recs[f"{t1}#1"]["url"].endswith("v12") and recs[f"{t1}#10"]["url"].endswith("v3")
    assert recs["top#1"]["url"].endswith("v99") and recs["top#1"]["have_skill"] == ""
    assert "winners" in recs["trick1#1"]["trick_use"]
    empty = examples.build([], [], "now", tricks=1, per=2, top=1)
    assert len(empty) == 3 and all(r["url"] == "" for r in empty)


def test_describe():
    s = examples.describe({"layout": "split_screen_facecam", "caption_style": "word_by_word",
                           "caption_position": "middle", "hook_text_on_screen": "BRO WHAT",
                           "sound_effects": ["impact", "none"], "cuts": 9})
    assert s == 'split screen facecam, captions word by word middle, hook "BRO WHAT", sfx impact, 9 cuts'


def test_playbook_rows_viral_tricks_and_new_trick_alert():
    hot = [{"key": "emoji_or_stickers", "name": "emoji", "have": False, "w": 0.6, "a": 0.1, "lift": 0.5},
           {"key": "screen_shake", "name": "screen shake", "have": True, "w": 0.5, "a": 0.2, "lift": 0.3},
           {"key": "loop_ending", "name": "loop ending", "have": True, "w": 0.3, "a": 0.1, "lift": 0.2},
           {"key": "freeze_frame", "name": "freeze frame", "have": True, "w": 0.2, "a": 0.1, "lift": 0.1}]
    entries = [e(i, i, emoji_or_stickers=True) for i in range(1, 6)]
    rows = {r["rule_id"]: r for r in examples.playbook_rows(hot, entries, "72 h", "now", n=3)}
    viral = examples.viral_from_playbook(rows.values())
    assert [t["key"] for t in viral] == ["screen_shake", "loop_ending", "freeze_frame"]   # only ones we can do
    alert = rows["alerts.new_trick"]
    assert "NEW VIRAL TRICK" in alert["rule"] and "emoji" in alert["rule"]
    assert alert["rule"].count("https://") == 3 and "v5" in alert["rule"]                # best breakouts
    calm = {r["rule_id"]: r for r in examples.playbook_rows(hot[1:], entries, "72 h", "now")}
    assert calm["alerts.new_trick"]["value"] == "null"
    assert examples.viral_from_playbook([]) == []


def test_alert_looks_at_the_top_5_with_2_links_and_keeps_its_first_day():
    hot = [{"key": "screen_shake", "name": "screen shake", "have": True, "w": 0.5, "a": 0.2, "lift": 0.3},
           {"key": "loop_ending", "name": "loop ending", "have": True, "w": 0.3, "a": 0.1, "lift": 0.2},
           {"key": "freeze_frame", "name": "freeze frame", "have": True, "w": 0.2, "a": 0.1, "lift": 0.1},
           {"key": "cuts_on_beat", "name": "cuts on the beat", "have": False, "w": 0.56, "a": 0.39, "lift": 0.17}]
    entries = [e(i, i, cuts_on_beat=True) for i in range(1, 6)]
    old = {r["rule_id"]: r for r in examples.playbook_rows(hot, entries, "72 h", "2026-11-28T09:00:00Z", n=3)}
    assert old["alerts.new_trick"]["value"] == "null"                       # top 3 only: no alert
    rows = {r["rule_id"]: r for r in examples.playbook_rows(hot, entries, "72 h", "2026-11-28T09:00:00Z", 3,
                                                             alert_top=5, links=2)}
    alert = rows["alerts.new_trick"]
    assert alert["rule"].startswith("NEW VIRAL TRICK") and "cuts on the beat (#4" in alert["rule"]
    assert alert["rule"].count("https://") == 2 and "since 2026-11-28" in alert["rule"]
    later = {r["rule_id"]: r for r in examples.playbook_rows(hot, entries, "72 h", "2026-11-30T09:00:00Z", 3,
                                                              alert_top=5, links=2, prev=alert)}
    assert json.loads(later["alerts.new_trick"]["value"])["since"] == "2026-11-28"   # same trick: same first day
    other = dict(alert, value=json.dumps({"key": "emoji_or_stickers", "since": "2026-11-20"}))
    fresh = {r["rule_id"]: r for r in examples.playbook_rows(hot, entries, "72 h", "2026-11-30T09:00:00Z", 3,
                                                              alert_top=5, links=2, prev=other)}
    assert json.loads(fresh["alerts.new_trick"]["value"])["since"] == "2026-11-30"   # new trick: new first day
