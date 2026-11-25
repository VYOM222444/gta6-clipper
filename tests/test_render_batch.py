

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
