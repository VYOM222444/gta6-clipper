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
    for name, job in [("scout.yml", "scout"), ("watcher.yml", "watch"), ("researcher.yml", "research"),
                      ("decider.yml", "decide")]:
        t = text(name)
        assert "cron:" in t and "  guard:" in t and f'.name=="{job}"' in t, name
        assert re.search(rf"  {job}:\n    needs: guard\n    if: needs.guard.outputs.skip != 'true'", t), name
        assert "from_trigger" in t and "actions: read" in t or "actions: write" in t


def test_chained_workflows_have_no_cron():
    for name in ("tracker.yml", "signals.yml", "gatekeeper.yml"):
        assert "cron:" not in text(name) and "workflow_dispatch:" in text(name)


def jobs(name):
    """{job name: its text} for one workflow file (the 2-space keys under `jobs:`)."""
    t = text(name)
    out, cur = {}, None
    for line in t[t.index("\njobs:\n") + 7:].splitlines():
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            cur = m.group(1)
            out[cur] = ""
        elif cur:
            out[cur] += line + "\n"
    return out


def test_jobs_that_commit_check_out_the_latest_main():
    # A run that waited behind another one (concurrency group) otherwise checks out the commit from
    # when it was started: it reads an old Gemini counter / old snapshots and its own commit is then
    # dropped as "already applied" or conflicts (27-28 Nov: real Researcher usage ~2x its counter).
    checkout = re.compile(r"uses: actions/checkout@v4\n\s+with:\n\s+ref: \$\{\{ github\.ref_name \}\}")
    for name in ("researcher.yml", "watcher.yml", "decider.yml", "tracker.yml"):
        committing = {j: b for j, b in jobs(name).items() if "git push" in b}
        assert committing, name
        for job, block in committing.items():
            assert checkout.search(block), (name, job)


def test_next_deep_run_starts_only_after_the_counter_is_pushed():
    deep = jobs("researcher.yml")["research-deep"]
    save, start = deep.index("name: Save Gemini call counter"), deep.index("name: Start the next deep run")
    assert save < start
    assert "if: success() && hashFiles('data/.deep_continue') != ''" in deep[start:]
