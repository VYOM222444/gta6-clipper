"""Workflow wiring: the chain Scout -> Tracker -> Signals -> Watcher and the guards."""
import re
from pathlib import Path

WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def text(name):
    return (WF / name).read_text(encoding="utf-8")


def test_chain_targets_exist_and_link_in_order():
    chain = {"scout.yml": "tracker.yml", "tracker.yml": "signals.yml", "signals.yml": "watcher.yml",
             "watcher.yml": "gatekeeper.yml"}
    for src, dst in chain.items():
        assert f"actions/workflows/{dst}/dispatches" in text(src), src
        assert (WF / dst).exists()
        assert "actions: write" in text(src)


def test_scheduled_workflows_have_a_guard():
    for name, job in [("scout.yml", "scout"), ("watcher.yml", "watch"), ("researcher.yml", "research")]:
        t = text(name)
        assert "cron:" in t and "  guard:" in t and f'.name=="{job}"' in t, name
        assert re.search(rf"  {job}:\n    needs: guard\n    if: needs.guard.outputs.skip != 'true'", t), name
        assert "from_trigger" in t and "actions: read" in t or "actions: write" in t


def test_chained_workflows_have_no_cron():
    for name in ("tracker.yml", "signals.yml", "gatekeeper.yml"):
        assert "cron:" not in text(name) and "workflow_dispatch:" in text(name)
