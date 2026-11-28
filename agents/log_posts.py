"""Log posted clips and keep their YouTube / Instagram / Facebook numbers up to date -> `Posted` tab.

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
         Instagram + Facebook posts (Meta Graph API, the brand Page tokens in META_TOKENS_JSON):
         the same columns plus reach_now / shares_now / saves_now / avg_watch_s, for posts
         younger than posts.meta_stats_days (+ one catch-up reading of older ones). Instagram
         links pasted by hand are found by their shortcode among the brand's latest posts.
         Needs the token permissions instagram_manage_insights, read_insights and
         pages_manage_engagement (without them: likes/comments only, and a hint in the log).

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

from agents import post_social
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


def numbers_update(row, now, views=None, likes=None, comments=None, published=None):
    """New Posted values from one reading of any platform: *_now, stats_at, posted_at and the
    frozen checkpoints. A number that is None (not available) is left as it was; without a view
    count nothing time-based is touched (stats_at pairs with views_now for the interpolation)."""
    upd = {}
    for col, v in (("likes_now", likes), ("comments_now", comments)):
        if v is not None:
            upd[col] = int(v)
    if published:
        upd["posted_at"] = published
    if views is None:
        return upd
    views = int(views)
    upd.update(views_now=views, stats_at=iso(now))
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
    if age_h >= 24 * 7 and str(row.get("likes_7d", "")) == "" and likes is not None:
        upd.update(likes_7d=int(likes), comments_7d=int(comments or 0))
    return upd


def update_stats(row, video, now):
    """New values for one YouTube Posted row, given its videos.list item."""
    st = video.get("statistics", {})
    return numbers_update(row, now, views=int(st.get("viewCount", 0)), likes=int(st.get("likeCount", 0) or 0),
                          comments=int(st.get("commentCount", 0) or 0),
                          published=video.get("snippet", {}).get("publishedAt"))


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
            "views_now", "likes_now", "comments_now", "stats_at", "views_6h", "views_12h", "views_48h",
            *SOCIAL_EXTRA)
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


# --- Instagram + Facebook (Meta Graph API, the brand Page tokens in META_TOKENS_JSON) ----------

# Meta renames metrics now and then: per platform, metric sets in order of preference; the first
# set Meta accepts is kept for the rest of the run. Watch times come in milliseconds.
IG_SETS = [("views", "reach", "saved", "shares", "ig_reels_avg_watch_time"), ("views",)]
FB_SETS = [("fb_reels_total_plays", "blue_reels_play_count", "post_impressions_unique",
            "post_video_avg_time_watched", "post_video_social_actions", "post_video_likes_by_reaction_type"),
           ("fb_reels_total_plays",), ("blue_reels_play_count",)]
SOCIAL_EXTRA = ("reach_now", "shares_now", "saves_now", "avg_watch_s")
PERMISSION_HINT = ("{p}: the Meta token can't read views yet. Add the permissions instagram_manage_insights, "
                   "read_insights and pages_manage_engagement, then redo steps 4-7 of the Instagram + Facebook "
                   "setup (CLAUDE.md section 10)")


def insight_values(data):
    """{metric: number} from a Graph insights answer; dict values (reactions by type) are summed."""
    out = {}
    for item in (data or {}).get("data") or []:
        v = None
        if item.get("values"):
            v = item["values"][0].get("value")
        elif isinstance(item.get("total_value"), dict):
            v = item["total_value"].get("value")
        if isinstance(v, dict):
            v = sum(x for x in v.values() if isinstance(x, (int, float)))
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[item.get("name")] = v
    return out


def error_kind(err):
    """'permission' / 'limit' / 'metric' / '' for a Meta error message."""
    m = str(err).lower()
    if "permission" in m or "(#10)" in m or "(#200)" in m:
        return "permission"
    if any(k in m for k in ("(#4)", "(#17)", "(#32)", "(#613)", "80001", "rate limit", "request limit",
                            "too many calls")):
        return "limit"
    return "metric" if "metric" in m else ""


def insights(meta, path, sets, state, key):
    """Metric values for one media / video. state[key] = index of the metric set in use."""
    if state.get(f"{key}_permission"):
        return {}
    while state.get(key, 0) < len(sets):
        try:
            return insight_values(meta.get(path, metric=",".join(sets[state.get(key, 0)])))
        except post_social.MetaError as e:
            kind = error_kind(e)
            if kind == "permission":
                state[f"{key}_permission"] = True
                return {}
            if kind != "metric":
                raise
            state[key] = state.get(key, 0) + 1     # one of these names is gone: try a smaller set
    return {}


def _secs(ms):
    return round(ms / 1000, 1) if isinstance(ms, (int, float)) else None


def _iso_or_none(s):
    try:
        return iso(parse_time(s)) if s else None
    except ValueError:
        return None


def ig_media_ids(meta, ig_user_id, limit=50):
    """{shortcode: media id} of the brand's latest Instagram posts (hand-pasted links only carry the
    shortcode; posts made by post_social already store the media id)."""
    if not ig_user_id:
        return {}
    data = meta.get(f"{ig_user_id}/media", fields="id,permalink", limit=limit)
    return {sc: str(m.get("id", "")) for m in data.get("data") or []
            if (sc := platform_id("instagram", m.get("permalink", "")))}


def ig_numbers(meta, media_id, state):
    info = meta.get(media_id, fields="like_count,comments_count,timestamp")
    v = insights(meta, f"{media_id}/insights", IG_SETS, state, "ig")
    return {"views": v.get("views"), "likes": info.get("like_count"), "comments": info.get("comments_count"),
            "published": _iso_or_none(info.get("timestamp")), "reach_now": v.get("reach"),
            "shares_now": v.get("shares"), "saves_now": v.get("saved"),
            "avg_watch_s": _secs(v.get("ig_reels_avg_watch_time"))}


def fb_numbers(meta, video_id, state):
    try:
        info = meta.get(video_id, fields="created_time,comments.summary(true).limit(0),likes.summary(true).limit(0)")
    except post_social.MetaError as e:
        if error_kind(e) == "limit":
            raise
        info = meta.get(video_id, fields="created_time")
    comments = ((info.get("comments") or {}).get("summary") or {}).get("total_count")
    likes = ((info.get("likes") or {}).get("summary") or {}).get("total_count")
    v = insights(meta, f"{video_id}/video_insights", FB_SETS, state, "fb")
    if likes is None:
        likes = v.get("post_video_likes_by_reaction_type")
    social = v.get("post_video_social_actions")             # comments + shares
    shares = max(int(social) - int(comments), 0) if social is not None and comments is not None else None
    return {"views": v.get("fb_reels_total_plays", v.get("blue_reels_play_count")), "likes": likes,
            "comments": comments, "published": _iso_or_none(info.get("created_time")),
            "reach_now": v.get("post_impressions_unique"), "shares_now": shares, "saves_now": None,
            "avg_watch_s": _secs(v.get("post_video_avg_time_watched"))}


def social_update(row, n, now):
    upd = numbers_update(row, now, views=n.get("views"), likes=n.get("likes"), comments=n.get("comments"),
                         published=n.get("published"))
    for col in SOCIAL_EXTRA:
        if n.get(col) is not None:
            upd[col] = n[col]
    return upd


def _wanted(row, now, days):
    """Young posts (checkpoints still open) + one catch-up reading of older posts never read."""
    try:
        if (now - parse_time(row.get("posted_at"))).total_seconds() <= days * 86400:
            return True
    except (TypeError, ValueError):
        return True
    return str(row.get("views_7d", "")) == "" and str(row.get("stats_at", "")) == ""


def meta_stats(ws, settings, now, tokens=None, make_meta=None):
    """Instagram + Facebook numbers of our posts -> Posted. Returns ({platform: rows updated}, notes),
    or (None, [why]) when Meta isn't set up. One failing post never stops the others."""
    if tokens is None:
        tokens = post_social.load_tokens()
    if not tokens:
        return None, ["not set up yet (secret META_TOKENS_JSON empty)"]
    version = settings.get("social", {}).get("graph_version", "v25.0")
    make_meta = make_meta or (lambda token: post_social.Meta(token, version))
    days = settings.get("posts", {}).get("meta_stats_days", 8)
    rows = [r for r in sheets.read_records(ws) if r.get("platform") in ("instagram", "facebook")
            and r.get("brand") in tokens and _wanted(r, now, days)]
    updates, counts, notes, state = {}, {"instagram": 0, "facebook": 0}, [], {}
    for brand in sorted({r["brand"] for r in rows}):
        tok = tokens[brand]
        meta, media_ids = make_meta(tok["token"]), None
        for r in [x for x in rows if x["brand"] == brand]:
            try:
                pid = str(r.get("platform_id") or "")
                if r["platform"] == "instagram":
                    if not pid.isdigit():
                        if media_ids is None:
                            media_ids = ig_media_ids(meta, tok.get("ig_user_id"))
                        pid = media_ids.get(pid or platform_id("instagram", r.get("url")), "")
                    if not pid:
                        notes.append(f"{r['post_id']}: not among the brand's latest Instagram posts")
                        continue
                    n = ig_numbers(meta, pid, state)
                else:
                    if not pid:
                        continue
                    n = fb_numbers(meta, pid, state)
            except post_social.MetaError as e:
                notes.append(f"{r['post_id']}: {str(e).replace(tok['token'], '***')[:140]}")
                if error_kind(e) == "limit":
                    notes.append(f"{brand}: Meta rate limit, the rest of this brand next run")
                    break
                continue
            upd = social_update(r, n, now)
            if upd:
                updates[r["post_id"]] = upd
                counts[r["platform"]] += 1
    if updates:
        sheets.set_values(ws, TABS[TAB], "post_id", updates)
    for key, name in (("ig", "Instagram"), ("fb", "Facebook")):
        if state.get(f"{key}_permission"):
            notes.append(PERMISSION_HINT.format(p=name))
        elif state.get(key, 0) >= len(IG_SETS if key == "ig" else FB_SETS):
            notes.append(f"{name}: Meta accepted none of the view metrics (renamed?): update "
                         f"{key.upper()}_SETS in agents/log_posts.py")
    return counts, notes


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
            try:
                counts, notes = meta_stats(ws, settings, now)
            except post_social.MetaError as e:
                counts, notes = None, [str(e)]
            if counts is None:
                print(f"Instagram/Facebook stats: {'; '.join(notes)}")
            else:
                for note in notes[:15]:
                    print(f"  {note}")
                print(f"Instagram stats updated for {counts['instagram']} posts, Facebook for "
                      f"{counts['facebook']}.")
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
