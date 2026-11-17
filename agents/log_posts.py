"""Log posted clips and keep their YouTube numbers up to date -> `Posted` tab.

Two jobs (both safe to re-run):
  log    posts/<date>.json (written by Claude from the links the owner pastes in chat)
         -> one Posted row per clip per platform (post_id = <batch_id>-<platform>).
         moment_id comes from the Batch tab. Existing rows keep their numbers.
  stats  for YouTube posts: views/likes/comments now (1 quota unit per 50 videos);
         checkpoints views_6h / 12h / 24h / 48h / 7d are frozen once a post passes that age.
         Stats only run every 6 h, so the value is interpolated between the last reading
         and this one (or from 0 views at posting time, if that's the only reading and it
         isn't too far past the checkpoint; otherwise it stays blank rather than wrong).
         likes_7d / comments_7d: first reading after 7 days. posted_at = YouTube publish time.
         Instagram has no free view API: those numbers are filled in by hand (or left blank).

posts/<date>.json format:
{"posted_at": "2026-11-16T14:00:00Z",          # used when a post has no exact time
 "posts": [{"batch_id": "2026-11-16-01", "brand": "neon_getaway",
            "platform": "youtube", "url": "https://youtube.com/shorts/ID"}]}

Run:  python -m agents.log_posts [log|stats|all]   (default all)
"""
import glob
import json
import re
import sys
from datetime import datetime, timezone

from lib import sheets
from lib.config import REPO_ROOT, load_settings
from lib.schema import TABS
from lib.youtube import YouTube, YouTubeError

TAB = "Posted"
YT_ID = re.compile(r"(?:shorts/|youtu\.be/|[?&]v=)([A-Za-z0-9_-]{11})")
IG_ID = re.compile(r"instagram\.com/(?:reel|reels|p)/([A-Za-z0-9_-]+)")


def platform_id(platform, url):
    m = (YT_ID if platform == "youtube" else IG_ID).search(url or "")
    return m.group(1) if m else ""


def parse_time(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rows_from_file(data, batch_rows):
    """Posted rows (dicts) from one posts/<date>.json. Unknown batch ids still log."""
    moment_of = {str(b.get("batch_id")): b.get("moment_id", "") for b in batch_rows}
    out = []
    for p in data.get("posts", []):
        platform = p["platform"].lower()
        out.append({
            "post_id": f"{p['batch_id']}-{platform}",
            "batch_id": p["batch_id"],
            "moment_id": moment_of.get(str(p["batch_id"]), ""),
            "brand": p["brand"],
            "platform": platform,
            "url": p["url"].split("?")[0],
            "posted_at": p.get("posted_at") or data.get("posted_at", ""),
            "platform_id": platform_id(platform, p["url"]),
        })
    return out


CHECKPOINTS = [(6, "views_6h"), (12, "views_12h"), (24, "views_24h"), (48, "views_48h"),
               (24 * 7, "views_7d")]


def checkpoint_value(cp, age, views, prev_age=None, prev_views=None):
    """Estimated views at `cp` hours, or None if not reached / no honest estimate."""
    if age < cp:
        return None
    if prev_age is not None and prev_views is not None and prev_age <= cp:
        a, va = prev_age, prev_views
    elif age <= 2 * cp:
        a, va = 0.0, 0
    else:
        return None
    if age <= a:
        return views
    return int(round(va + (views - va) * (cp - a) / (age - a)))


def update_stats(row, video, now):
    """New values for one YouTube Posted row, given its videos.list item."""
    st = video.get("statistics", {})
    views = int(st.get("viewCount", 0))
    likes = int(st.get("likeCount", 0) or 0)
    comments = int(st.get("commentCount", 0) or 0)
    published = video.get("snippet", {}).get("publishedAt")
    upd = {"views_now": views, "likes_now": likes, "comments_now": comments, "stats_at": iso(now)}
    if published:
        upd["posted_at"] = published
    start = parse_time(published or row.get("posted_at") or iso(now))
    age_h = (now - start).total_seconds() / 3600
    prev_age = prev_views = None
    if str(row.get("stats_at", "")) and str(row.get("views_now", "")) != "":
        prev_age = (parse_time(row["stats_at"]) - start).total_seconds() / 3600
        prev_views = int(float(row["views_now"]))
    for cp, col in CHECKPOINTS:
        if str(row.get(col, "")) == "":
            v = checkpoint_value(cp, age_h, views, prev_age, prev_views)
            if v is not None:
                upd[col] = v
    if age_h >= 24 * 7 and str(row.get("likes_7d", "")) == "":
        upd.update(likes_7d=likes, comments_7d=comments)
    return upd


def log(ws, batch_rows, paths):
    rows = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            rows += rows_from_file(json.load(f), batch_rows)
    keep = ("posted_at", "views_24h", "views_7d", "likes_7d", "comments_7d", "final_label",
            "views_now", "likes_now", "comments_now", "stats_at", "views_6h", "views_12h", "views_48h")
    return sheets.upsert_rows(ws, TABS[TAB], "post_id", rows, preserve=keep)


def stats(ws, yt, now):
    rows = [r for r in sheets.read_records(ws) if r.get("platform") == "youtube" and r.get("platform_id")]
    if not rows:
        return 0
    videos = {v["id"]: v for v in yt.videos([r["platform_id"] for r in rows], parts="statistics,snippet")}
    updates = {r["post_id"]: update_stats(r, videos[r["platform_id"]], now)
               for r in rows if r["platform_id"] in videos}
    sheets.set_values(ws, TABS[TAB], "post_id", updates)
    return len(updates)


def main(argv=None):
    what = (argv or sys.argv[1:] or ["all"])[0]
    settings = load_settings()
    now = datetime.now(timezone.utc)
    try:
        ss = sheets.open_sheet(sheets.get_client(), settings["sheet"]["id"])
        sheets.ensure_tab(ss, TAB, TABS[TAB])
        ws = ss.worksheet(TAB)
        if what in ("log", "all"):
            paths = sorted(glob.glob(str(REPO_ROOT / "posts" / "*.json")))
            upd, new = log(ws, sheets.read_records(ss.worksheet("Batch")), paths)
            print(f"Posted: {new} new, {upd} updated from {len(paths)} file(s).")
        if what in ("stats", "all"):
            yt = YouTube(budget_units=20)
            n = stats(ws, yt, now)
            print(f"YouTube stats updated for {n} posts ({yt.used} quota units).")
    except sheets.SheetSetupError as e:
        print(f"LOG POSTS FAILED (sheet): {e}")
        return 1
    except YouTubeError as e:
        print(f"LOG POSTS FAILED (YouTube): {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
