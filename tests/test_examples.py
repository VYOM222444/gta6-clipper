from lib import examples


def e(i, breakout, **f):
    return ({"url": f"https://youtube.com/shorts/v{i}", "title": f"t{i}", "breakout": str(breakout),
             "views": "1000", "channel_title": "c"}, f)


def test_lists_per_trick_best_breakouts_first_with_fixed_slots():
    entries = [e(1, 3, screen_shake=True), e(2, 12, screen_shake=True, layout="full_vertical"),
               e(3, 7, emoji_or_stickers=True)]
    recs = {r["example_id"]: r for r in examples.build(entries, "now")}
    assert recs["TOP breakouts (any style)#1"]["url"].endswith("v2")
    assert recs["screen shake#1"]["url"].endswith("v2") and recs["screen shake#2"]["url"].endswith("v1")
    assert recs["screen shake#3"]["url"] == "" and recs["screen shake#3"]["have_skill"] == ""
    assert recs["emoji / sticker pop-ups#1"]["have_skill"] == "no"
    assert "full vertical" in recs["screen shake#1"]["style"] and "shake" in recs["screen shake#1"]["style"]


def test_describe():
    s = examples.describe({"layout": "split_screen_facecam", "caption_style": "word_by_word",
                           "caption_position": "middle", "hook_text_on_screen": "BRO WHAT",
                           "sound_effects": ["impact", "none"], "cuts": 9})
    assert s == 'split screen facecam, captions word by word middle, hook "BRO WHAT", sfx impact, 9 cuts'
