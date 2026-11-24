from lib import skills


def f(**kw):
    base = {"emoji_or_stickers": False, "loop_ending": False, "zoom_ins": 0, "sound_effects": ["none"],
            "layout": "blurred_background", "background_music": True}
    base.update(kw)
    return base


def test_report_ranks_missing_tricks_winners_use_more():
    win = [f(emoji_or_stickers=True, zoom_ins=3)] * 6 + [f(loop_ending=True)] * 3 + [f()] * 1
    avg = [f(zoom_ins=2)] * 2 + [f()] * 8
    g = skills.report(win, avg)
    by = {x["key"]: x for x in g}
    assert by["emoji_or_stickers"]["verdict"] == "BUILD NEXT"          # 60% vs 0%, can't do
    assert by["loop_ending"]["verdict"] == "BUILD NEXT"          # 30% vs 0%
    assert by["zoom_ins"]["verdict"] == "HAVE IT"
    assert by["background_music"]["verdict"] == "NOT FOR US"
    assert g[0]["key"] == "emoji_or_stickers" and g[1]["key"] == "loop_ending"   # biggest gap first


def test_small_or_equal_use_is_skipped_and_too_few_winners_gives_nothing():
    win = [f(emoji_or_stickers=True)] * 2 + [f()] * 8
    avg = [f(emoji_or_stickers=True)] * 2 + [f()] * 8
    assert {x["key"]: x for x in skills.report(win, avg)}["emoji_or_stickers"]["verdict"] == "SKIP"
    assert skills.report([f()] * 3, [f()] * 3) == []


def test_playbook_rows():
    rows = skills.playbook_rows(skills.report([f(emoji_or_stickers=True)] * 10, [f()] * 10), "src", "now")
    assert rows[0]["rule_id"] == "gaps.emoji_or_stickers"
    assert rows[0]["rule"].startswith("#1 BUILD NEXT: emoji / sticker pop-ups - winners 100% vs average 0%")
    assert rows[0]["value"] == "+1.00"
