from lib.config import load_blocklist, load_rules, load_settings, load_watchlist


def test_settings_load_and_have_essentials():
    s = load_settings()
    assert s["sheet"]["id"]
    assert len(s["brands"]) == 5
    w = s["gemini"]["watcher"]
    assert w["daily_budget"] <= w["daily_request_cap"]
    b = s["gemini"]["bulk"]
    assert b["daily_budget"] <= b["daily_request_cap"]
    assert s["youtube"]["daily_budget_units"] <= s["youtube"]["daily_quota_units"]


def test_rules_load():
    r = load_rules()
    assert r["tracker"]["rising_velocity_ratio"] > 1
    assert r["signals"]["chat_lead_seconds"] > 0
    assert r["signals"]["reaction_words"]


def test_lists_load():
    assert load_watchlist()["creators"] == []
    assert load_blocklist()["channels"] == []
