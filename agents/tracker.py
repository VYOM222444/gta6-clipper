"""Tracker: snapshot every tracked video every 6 hours -> data/snapshots.csv + `Tracker` tab.

Tracks every Candidates row except blocked ones, until the video is 7 days old (Mature)
or has slowed down for 3 snapshots in a row. Labels: New, Watching, Rising, Breakout,
Steady, Fading, Mature, Gone. Thresholds come from playbook/rules.json -> tracker.

Run:  python -m agents.tracker    (needs YOUTUBE_API_KEY, GOOGLE_SERVICE_ACCOUNT_JSON)
The GitHub workflow commits data/snapshots.csv back to the repo after each run.
"""
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

from lib import sheets
from lib.config import REPO_ROOT, load_rules, load_settings
from lib.schema import TABS
from lib.youtube import YouTube, YouTubeError

TAB = "Tracker"
SNAPSHOTS = REPO_ROOT / "data" / "snapshots.csv"
SNAP_FIELDS = ["snapshot_at", "video_id", "views", "likes", "comments"]
SKIP_STATUS = {"blocked_leak", "blocked"}
STOPPED = {"stopped", "gone"}


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# --- snapshot log ------------------------------------------------------------------

def load_history(path=SNAPSHOTS):
    hist = {}
    p = Path(path)
    if not p.exists():
        return hist
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            hist.setdefault(row["video_id"], []).append({
                "t": parse_time(row["snapshot_at"]),
                "views": int(row["views"] or 0),
                "likes": int(row["likes"] or 0),
                "comments": int(row["comments"] or 0),
            })
    for snaps in hist.values():
        snaps.sort(key=lambda s: s["t"])
    return hist


def append_snapshots(rows, path=SNAPSHOTS):
    p = Path(path)
    new_file = not p.exists() or p.stat().st_size == 0
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SNAP_FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)


# --- metrics (pure, unit-tested) ---------------------------------------------------

def velocities(snaps, published):
    """views/hour for each snapshot: first one vs publish time, then vs previous."""
    out = []
    for i, s in enumerate(snaps):
        if i == 0:
            hours = max((s["t"] - published).total_seconds() / 3600, 1.0)
            out.append(s["views"] / hours)
        else:
            prev = snaps[i - 1]
            hours = max((s["t"] - prev["t"]).total_seconds() / 3600, 1e-6)
            out.append(max(s["views"] - prev["views"], 0) / hours)
    return out


def slowing_streak(vels):
    n = 0
    for i in range(len(vels) - 1, 0, -1):
        if vels[i] < vels[i - 1]:
            n += 1
        else:
            break
    return n


def compute(snaps, published, subscribers, now, cfg):
    """Metrics + label for one video from its snapshot history (oldest first)."""
    last = snaps[-1]
    age_h = (now - published).total_seconds() / 3600
    vels = velocities(snaps, published)
    vel = vels[-1]
    accel = vels[-1] / vels[-2] if len(vels) >= 2 and vels[-2] > 0 else None
    views = last["views"]
    engagement = (last["likes"] + last["comments"]) / views if views else 0.0
    reach = views / subscribers if subscribers else None
    streak = slowing_streak(vels)

    tracking = "active"
    if age_h >= cfg["mature_days"] * 24:
        label, tracking = "Mature", "stopped"
    elif age_h < cfg["new_hours"]:
        label = "New"
    elif streak >= cfg["stop_after_fading_snapshots"]:
        label, tracking = "Fading", "stopped"
    elif streak >= cfg["fading_snapshots"]:
        label = "Fading"
    elif accel is None:
        label = "Watching"  # only one snapshot so far: no trend yet
    elif accel >= cfg["rising_velocity_ratio"]:
        big = subscribers and views >= cfg["breakout_views_vs_subscribers"] * subscribers
        label = "Breakout" if big else "Rising"
    else:
        label = "Steady"

    return {
        "age_h": round(age_h, 1),
        "views": views,
        "likes": last["likes"],
        "comments": last["comments"],
        "velocity": round(vel, 1),
        "acceleration": "" if accel is None else round(accel, 2),
        "engagement": round(engagement, 4),
        "reach_score": "" if reach is None else round(reach, 3),
        "label": label,
        "snapshots": len(snaps),
        "last_snapshot_at": iso(last["t"]),
        "tracking": tracking,
    }


def plan(candidates, tracker_rows):
    """Which candidate rows to snapshot this run."""
    stopped = {r["video_id"] for r in tracker_rows if r.get("tracking") in STOPPED}
    out, seen = [], set()
    for c in candidates:
        vid = str(c.get("video_id", ""))
        if not vid or vid in seen or vid in stopped or c.get("status") in SKIP_STATUS:
            continue
        seen.add(vid)
        out.append(c)
    return out


def run(yt, candidates, tracker_rows, history, cfg, now):
    """Returns (tracker_records, new_snapshot_rows)."""
    todo = plan(candidates, tracker_rows)
    items = {v["id"]: v for v in yt.videos([c["video_id"] for c in todo],
                                            parts="statistics,liveStreamingDetails")} if todo else {}
    gap_h = cfg.get("min_snapshot_gap_hours", 1)
    records, new_snaps = [], []
    for c in todo:
        vid = str(c["video_id"])
        base = {"video_id": vid, "title": c.get("title", ""),
                "channel_title": c.get("channel_title", ""), "kind": c.get("kind", ""),
                "url": c.get("url", "")}
        v = items.get(vid)
        snaps = list(history.get(vid, []))
        if v is None:  # deleted, private or region-blocked
            records.append({**base, "label": "Gone", "tracking": "gone",
                            "snapshots": len(snaps)})
            continue
        live = v.get("liveStreamingDetails")
        if live:
            base["kind"] = "live_vod" if live.get("actualEndTime") else "live_now"
        st = v.get("statistics", {})
        snap = {"t": now, "views": int(st.get("viewCount", 0)),
                "likes": int(st.get("likeCount", 0) or 0),
                "comments": int(st.get("commentCount", 0) or 0)}
        if not snaps or (now - snaps[-1]["t"]).total_seconds() / 3600 >= gap_h:
            snaps.append(snap)
            new_snaps.append({"snapshot_at": iso(now), "video_id": vid, "views": snap["views"],
                              "likes": snap["likes"], "comments": snap["comments"]})
        else:
            snaps[-1] = {**snap, "t": snaps[-1]["t"]}  # too soon: refresh numbers only
        subs = _num(c.get("subscribers"))
        records.append({**base, **compute(snaps, parse_time(c["published_at"]), subs, now, cfg)})
    return records, new_snaps


def main():
    settings, rules = load_settings(), load_rules()
    now = datetime.now(timezone.utc)
    try:
        client = sheets.get_client()
        ss = sheets.open_sheet(client, settings["sheet"]["id"])
        sheets.ensure_tab(ss, TAB, TABS[TAB])
        cand_ws, trk_ws = ss.worksheet("Candidates"), ss.worksheet(TAB)
        candidates = sheets.read_records(cand_ws)
        tracker_rows = sheets.read_records(trk_ws)
    except sheets.SheetSetupError as e:
        print(f"TRACKER FAILED (sheet): {e}")
        return 1

    try:
        yt = YouTube(budget_units=settings["youtube"]["tracker_run_budget_units"])
        records, new_snaps = run(yt, candidates, tracker_rows, load_history(), rules["tracker"], now)
    except YouTubeError as e:
        print(f"TRACKER FAILED (YouTube): {e}")
        return 1

    append_snapshots(new_snaps)
    upd, new = sheets.upsert_rows(trk_ws, TABS[TAB], "video_id", records)
    counts = {}
    for r in records:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    print(f"Tracked {len(records)} videos ({new} new rows, {upd} updated); "
          f"{len(new_snaps)} snapshots saved.")
    print("Labels: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    for r in sorted(records, key=lambda r: r.get("velocity") or 0, reverse=True)[:5]:
        print(f"  fastest: {r.get('velocity', 0):>10} v/h | {r['label']:<8} | {r['title'][:60]}")
    print(f"Quota used this run: {yt.used}/{yt.budget} units.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
