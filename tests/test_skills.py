from lib import skills


def f(**kw):
    base = {"screen_shake": False, "freeze_frame": False, "zoom_ins": 0, "sound_effects": ["none"],
            "layout": "blurred_background", "background_music": True}
    base.update(kw)
    return base


def test_report_ranks_missing_tricks_winners_use_more():
    win = [f(screen_shake=True, zoom_ins=3)] * 6 + [f(freeze_frame=True)] * 3 + [f()] * 1
    avg = [f(zoom_ins=2)] * 2 + [f()] * 8
    g = skills.report(win, avg)
    by = {x["key"]: x for x in g}
    assert by["screen_shake"]["verdict"] == "BUILD NEXT"          # 60% vs 0%, can't do
    assert by["freeze_frame"]["verdict"] == "BUILD NEXT"          # 30% vs 0%
    assert by["zoom_ins"]["verdict"] == "HAVE IT"
    assert by["background_music"]["verdict"] == "NOT FOR US"
    assert g[0]["key"] == "screen_shake" and g[1]["key"] == "freeze_frame"   # biggest gap first


def test_small_or_equal_use_is_skipped_and_too_few_winners_gives_nothing():
    win = [f(screen_shake=True)] * 2 + [f()] * 8
    avg = [f(screen_shake=True)] * 2 + [f()] * 8
    assert {x["key"]: x for x in skills.report(win, avg)}["screen_shake"]["verdict"] == "SKIP"
    assert skills.report([f()] * 3, [f()] * 3) == []


def test_playbook_rows():
    rows = skills.playbook_rows(skills.report([f(screen_shake=True)] * 10, [f()] * 10), "src", "now")
    assert rows[0]["rule_id"] == "gaps.screen_shake"
    assert rows[0]["rule"].startswith("#1 BUILD NEXT: screen shake - winners 100% vs average 0%")
    assert rows[0]["value"] == "+1.00"
