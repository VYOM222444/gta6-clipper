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
