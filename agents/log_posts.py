"""Log posted clips and keep their YouTube numbers up to date -> `Posted` tab.

Three jobs (all safe to re-run):
  discover  finds new uploads on OUR 5 YouTube channels (settings.brands[].youtube handles) and
         matches each to a Batch row of that brand by title (fuzzy, >= posts.match_min) ->
         Posted rows, so the owner no longer pastes YouTube links. ~3 units per brand.
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

Run:  python -m agents.log_posts [log|discover|stats|all]   (stats also runs discover)   (default all)
"""
import difflib
import glob
import json
import re
import sys
from datetime import datetime, timezone

from lib import experiments, sheets
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


def norm_title(t):
    t = re.sub(r"#\w+", " ", str(t).lower())
    return " ".join(re.sub(r"[^0-9a-z\u0900-\u097f]+", " ", t).split())


def match_uploads(uploads, batch_rows, brand, posted_rows, min_ratio=0.75):
    """Posted rows for uploads of one brand's channel that match an unlogged Batch row's title.
    uploads: videos.list items (snippet). Greedy: best title match first; each side used once."""
    logged_ids = {str(p.get("platform_id")) for p in posted_rows if p.get("platform") == "youtube"}
    logged_batches = {str(p.get("batch_id")) for p in posted_rows if p.get("platform") == "youtube"}
    open_rows = [b for b in batch_rows if b.get("brand") == brand and b.get("title")
                 and str(b.get("batch_id")) not in logged_batches
                 and str(b.get("approved", "yes")).lower() != "no"]
    pairs = []
    for v in uploads:
        if v["id"] in logged_ids:
            continue
        sn = v.get("snippet", {})
        pub = sn.get("publishedAt", "")
        for b in open_rows:
            day = str(b.get("batch_id"))[:10]
            if pub and day and pub[:10] < day:  # can't be posted before its batch day (UTC vs IST: same or later)
                continue
            r = difflib.SequenceMatcher(None, norm_title(sn.get("title")), norm_title(b["title"])).ratio()
            if r >= min_ratio:
                pairs.append((r, v, b))
    pairs.sort(key=lambda x: x[0], reverse=True)
    used_v, used_b, out = set(), set(), []
    for r, v, b in pairs:
        if v["id"] in used_v or b["batch_id"] in used_b:
            continue
        used_v.add(v["id"])
        used_b.add(b["batch_id"])
        out.append({"post_id": f"{b['batch_id']}-youtube", "batch_id": b["batch_id"],
                    "moment_id": b.get("moment_id", ""), "brand": brand, "platform": "youtube",
                    "url": f"https://youtube.com/shorts/{v['id']}",
                    "posted_at": v.get("snippet", {}).get("publishedAt", ""), "platform_id": v["id"]})
    return out


def discover(ws, yt, batch_rows, settings):
    """New uploads on our own channels -> Posted rows. Returns (rows written, notes)."""
    cfg = settings.get("posts", {})
    posted = sheets.read_records(ws)
    rows, notes = [], []
    for b in settings["brands"]:
        handle = b.get("youtube", "")
        if not handle:
            continue
        cid = yt.channel_id_for_handle(handle)
        if not cid:
            notes.append(f"{handle}: channel not found")
            continue
        ids = yt.recent_uploads(cid, cfg.get("uploads_per_channel", 50))
        uploads = yt.videos(ids, parts="snippet") if ids else []
        found = match_uploads(uploads, batch_rows, b["key"], posted, cfg.get("match_min", 0.75))
        unmatched = len([v for v in uploads if v["id"] not in {str(p.get("platform_id")) for p in posted}]) - len(found)
        notes.append(f"{handle}: {len(uploads)} recent uploads, {len(found)} new matched"
                     + (f", {unmatched} not matched to a batch clip" if unmatched > 0 else ""))
        rows += found
    if rows:
        sheets.upsert_rows(ws, TABS[TAB], "post_id", rows, preserve=("posted_at",))
    return len(rows), notes


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


def experiment_report(ss, ws, settings, now):
    """Skills under test: our posts with vs without, at the earliest solid checkpoint (6/12/24 h)."""
    skills = list((settings["render"].get("edits", {}).get("experiments") or {}).keys())
    if (settings["render"].get("reframe") or {}).get("enabled"):
        skills += ["split", "follow"]
    res = experiments.report(sheets.read_records(ws), sheets.read_records(ss.worksheet("Batch")), skills)
    if not res:
        print("Experiments: not enough posts with/without each skill yet (need 3 + 3 with 6 h views).")
        return
    sheets.ensure_tab(ss, "Playbook", TABS["Playbook"])
    rows = experiments.playbook_rows(res, iso(now))
    sheets.upsert_rows(ss.worksheet("Playbook"), TABS["Playbook"], "rule_id", rows)
    print("Experiments (our own posts):")
    for r in rows:
        print(f"  {r['rule']}")


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
        if what in ("stats", "all", "discover"):
            yt = YouTube(budget_units=60)
            n, notes = discover(ws, yt, sheets.read_records(ss.worksheet("Batch")), settings)
            for note in notes:
                print(f"  {note}")
            print(f"Auto-logged {n} new YouTube post(s) from our channels.")
        if what in ("stats", "all"):
            n = stats(ws, yt, now)
            print(f"YouTube stats updated for {n} posts ({yt.used} quota units).")
            experiment_report(ss, ws, settings, now)
    except sheets.SheetSetupError as e:
        print(f"LOG POSTS FAILED (sheet): {e}")
        return 1
    except YouTubeError as e:
        print(f"LOG POSTS FAILED (YouTube): {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
