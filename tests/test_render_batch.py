

def test_to_render_newest_first_and_skips_stale():
    from datetime import date
    from agents.render_batch import to_render
    rows = [{"batch_id": f"2026-11-{d}-0{i}", "approved": "yes", "render_status": ""}
            for d in (20, 24, 25) for i in (1, 2)]
    rows.append({"batch_id": "2026-11-25-03", "approved": "no", "render_status": ""})
    rows.append({"batch_id": "2026-11-25-04", "approved": "yes", "render_status": "done"})
    got = [r["batch_id"] for r in to_render(rows, 10, 1, today=date(2026, 11, 25))]
    assert got == ["2026-11-25-01", "2026-11-25-02", "2026-11-24-01", "2026-11-24-02"]
    assert len(to_render(rows, 10, None, today=date(2026, 11, 25))) == 6
    assert len(to_render(rows, 3, 1, today=date(2026, 11, 25))) == 3


def test_skill_test_renders_each_clip_without_and_with_the_skills(monkeypatch, tmp_path):
    from agents import render_batch as rb
    calls = []

    def fake(row, cfg, transcriber, day, js, ecfg, style, test):
        calls.append((row["batch_id"], test["force"], test["out"].name, test["out"].parent.parent.name))
        return {"render_status": "done", "output_path": str(test["out"])}
    monkeypatch.setattr(rb, "render_one", fake)
    rows = [{"batch_id": "2026-11-27-01", "approved": "yes", "render_status": "done"},
            {"batch_id": "2026-11-28-02", "approved": "yes"},
            {"batch_id": "2026-11-28-03", "approved": "no"}]
    n = rb.run_skill_test(["deadair", "memesfx"], rows, {"output_dir": str(tmp_path)}, None, None, {}, {}, 3)
    assert n == 2
    assert calls[0] == ("2026-11-28-02", {"deadair": False, "memesfx": False}, "2026-11-28-02__base.mp4",
                        "_skill_tests")
    assert calls[1][1:3] == ({"deadair": True, "memesfx": True}, "2026-11-28-02__deadair+memesfx.mp4")
    assert [c[0] for c in calls[2:]] == ["2026-11-27-01"] * 2      # clips rendered before can be tested too


def test_main_skill_test_mode_leaves_the_batch_tab_alone(monkeypatch):
    from agents import render_batch as rb
    from lib import sheets
    from lib.schema import TABS
    from tests.fakes import FakeSpreadsheet, FakeWorksheet
    head = TABS["Batch"]
    ws = FakeWorksheet("Batch", [head, [("2026-11-28-01" if h == "batch_id" else "yes" if h == "approved" else "")
                                        for h in head]])
    monkeypatch.setattr(sheets, "get_client", lambda: None)
    monkeypatch.setattr(sheets, "open_sheet", lambda c, i: FakeSpreadsheet([ws]))
    monkeypatch.setattr(rb, "Transcriber", lambda size: None)
    monkeypatch.setattr(rb, "read_playbook", lambda: None)
    monkeypatch.setattr(rb, "prepare_edits", lambda cfg, work, rows=None: {"flags": {}})
    got = {}
    monkeypatch.setattr(rb, "run_skill_test", lambda skills, rows, cfg, tr, js, ecfg, style, limit:
                        got.update(skills=skills, limit=limit, n=len(rows)) or 1)
    monkeypatch.setattr(sheets, "set_values", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Batch written")))
    monkeypatch.setenv("CLIPPER_TEST_SKILLS", "deadair, bogus")
    monkeypatch.setenv("CLIPPER_MAX_CLIPS", "0")
    assert rb.main() == 0
    assert got == {"skills": ["deadair"], "limit": 3, "n": 1}


def test_use_skills_switches_them_on_for_this_render_only(monkeypatch):
    from datetime import datetime, timezone
    from agents import render_batch as rb
    from lib import sheets
    from lib.schema import TABS
    from tests.fakes import FakeSpreadsheet, FakeWorksheet
    head = TABS["Batch"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = {"batch_id": f"{today}-01", "approved": "yes", "brand": "wasted_again"}
    ws = FakeWorksheet("Batch", [head, [row.get(h, "") for h in head]])
    monkeypatch.setattr(sheets, "get_client", lambda: None)
    monkeypatch.setattr(sheets, "open_sheet", lambda c, i: FakeSpreadsheet([ws]))
    monkeypatch.setattr(rb, "Transcriber", lambda size: None)
    monkeypatch.setattr(rb, "read_playbook", lambda: None)
    monkeypatch.setattr(rb, "prepare_style", lambda cfg, rows: {})
    monkeypatch.setattr(rb, "prepare_edits", lambda cfg, work, rows=None: {"flags": {}})
    seen = []
    monkeypatch.setattr(rb, "render_one", lambda r, cfg, tr, day, js, ecfg, style, test=None:
                        seen.append((r["batch_id"], dict(ecfg.get("force") or {}), test)) or {"render_status": "done"})
    monkeypatch.delenv("CLIPPER_TEST_SKILLS", raising=False)
    monkeypatch.setenv("CLIPPER_USE_SKILLS", "deadair,memesfx,nope")
    assert rb.main() == 0
    assert seen == [(f"{today}-01", {"deadair": True, "memesfx": True}, None)]   # a normal render, not a test
    assert ws.get_all_records()[0]["render_status"] == "done"                     # Batch updated as usual
