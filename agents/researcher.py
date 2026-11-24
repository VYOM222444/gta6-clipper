"""Researcher: what do WINNING GTA 6 Shorts do that average ones don't? -> Playbook findings.

Runs once a day (GitHub Actions). Three steps, each safe to re-run:

1. collect  YouTube search for GTA 6 Shorts that are 3-14 days old (views have settled),
            several queries x 2 orders (~1,200 quota units). Each Short gets a score
            (views, plus a bonus for beating its channel size) and a label within today's
            cohort: winner (top 15%), average (30-70th percentile), low (bottom 30%),
            above (the rest). -> `Research` tab, one row per Short.
2. analyze  Gemini Flash-Lite watches ~20 winners + ~20 average Shorts not analysed yet,
            BLIND (no views given), and tags the editing: hook, first second, captions,
            layout, zooms, cuts, sound effects, slow-mo, loop ending, payoff second...
            -> `features` (JSON) on the Research row.
3. findings Compares winners vs average over the last `window_days` of analysed Shorts:
            share of each yes/no feature, medians of numbers, most common styles, title /
            hashtag patterns, most-clipped creators, today's winning topics.
            -> `Playbook` tab rows (section research.*). Claude turns strong findings into
            PLAYBOOK.md / template changes.

Fresh tier ("what works RIGHT NOW"): the same three steps for Shorts 0-72 h old. They are
compared by views per hour against Shorts of the same age (0-6 h vs 0-6 h ...), plus the
channel-size bonus, and get a provisional `fresh_label` (rising / fresh_average / fresh_low /
fresh_above). Scout's own Shorts (Candidates tab, kind=short) are added for free. Findings
go to Playbook rows research.now.*; a "SHIFT" note marks a feature where the 72 h verdict
differs from the 7-day one (the trend is changing). Hot topic words of rising Shorts
(research.now.topics) are read by Scout to rank matching videos higher. The final
winner/average label still comes from the settled (3-14 day) tier; analysed features are
reused when a fresh Short grows old enough.

Run:  python -m agents.researcher [collect|analyze|findings|fresh|all]   (default all)
      fresh = collect + analyze + findings for the fresh tier only (afternoon run).
Needs YOUTUBE_API_KEY, GEMINI_API_KEY, GOOGLE_SERVICE_ACCOUNT_JSON.
"""
import json
import math
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from agents.scout import parse_iso_duration, title_ok
from lib import keywords, sheets
from lib import style as clip_style
from lib.config import REPO_ROOT, load_rules, load_settings
from lib.gemini import BudgetReached, Gemini, GeminiError, Transient, Usage
from lib.schema import TABS
from lib.youtube import QuotaExceeded, YouTube, YouTubeError

TAB = "Research"
USAGE_FILE = REPO_ROOT / "data" / "gemini_usage_researcher.json"
CONTENT_TYPES = ["chaos", "reaction", "news", "secret", "tip", "other"]
HOOKS = ["action_first", "text_question", "text_statement", "face_reaction", "voice_hook",
         "countdown_or_list", "before_after", "none"]
HOOK_TEXT_KINDS = ["question", "bold_claim", "reaction", "warning", "number_or_list", "pov", "none"]
SFX = ["riser", "impact", "whoosh", "record_scratch", "meme_sound", "ding", "none"]
MUSIC = ["none", "phonk", "hype_edm", "hip_hop_rap", "epic_orchestral", "lofi_chill", "meme_song",
         "sad_emotional", "other"]
FRESH_LABELS = ("rising", "fresh_average")
HASHTAG = re.compile(r"#(\w+)", re.UNICODE)
HANDLE = re.compile(r"(?<![\w.])@([A-Za-z0-9_.\-]{3,30})")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "hook_type": {"type": "STRING", "enum": HOOKS},
        "text_on_screen_first_second": {"type": "BOOLEAN"},
        "hook_text_on_screen": {"type": "STRING"},
        "hook_text_kind": {"type": "STRING", "enum": HOOK_TEXT_KINDS},
        "payoff_second": {"type": "NUMBER"},
        "has_title_label": {"type": "BOOLEAN"},
        "caption_style": {"type": "STRING", "enum": ["none", "word_by_word", "phrase_by_phrase", "static_text"]},
        "caption_position": {"type": "STRING", "enum": ["top", "middle", "bottom", "none"]},
        "caption_keyword_color": {"type": "BOOLEAN"},
        "layout": {"type": "STRING", "enum": ["full_vertical", "blurred_background",
                                              "split_screen_facecam", "black_bars", "other"]},
        "facecam": {"type": "BOOLEAN"},
        "zoom_ins": {"type": "INTEGER"},
        "cuts": {"type": "INTEGER"},
        "slow_motion": {"type": "BOOLEAN"},
        "freeze_frame": {"type": "BOOLEAN"},
        "replay_of_moment": {"type": "BOOLEAN"},
        "screen_shake": {"type": "BOOLEAN"},
        "sound_effects": {"type": "ARRAY", "items": {"type": "STRING", "enum": SFX}},
        "background_music": {"type": "BOOLEAN"},
        "music_type": {"type": "STRING", "enum": MUSIC},
        "cuts_on_beat": {"type": "BOOLEAN"},
        "voiceover": {"type": "BOOLEAN"},
        "emoji_or_stickers": {"type": "BOOLEAN"},
        "loop_ending": {"type": "BOOLEAN"},
        "ends_right_after_payoff": {"type": "BOOLEAN"},
        "pace": {"type": "STRING", "enum": ["slow", "medium", "fast"]},
        "content_type": {"type": "STRING", "enum": CONTENT_TYPES},
        "topic": {"type": "STRING"},
        "spoken_language": {"type": "STRING"},
    },
    "required": ["hook_type", "text_on_screen_first_second", "payoff_second", "caption_style",
                 "layout", "facecam", "zoom_ins", "cuts", "sound_effects", "background_music",
                 "music_type", "loop_ending", "pace", "content_type", "topic"],
}

PROMPT = """You are an editing analyst. Watch this YouTube Short about GTA 6 and describe HOW it is
edited, exactly as you see and hear it. Do not judge whether it is good.
- hook_type: what grabs attention in the first 2 seconds (action_first = the action is already
  happening; text_question / text_statement = big on-screen text; face_reaction = a face
  reacting; voice_hook = someone speaking a hook; countdown_or_list; before_after; none).
- text_on_screen_first_second: is there readable on-screen text in the first second?
- hook_text_on_screen: the exact big headline/hook text shown in the first 2 seconds (not
  subtitles of speech, not a channel name), copied word for word; "" if there is none.
- hook_text_kind: what that hook text is (question, bold_claim, reaction like "BRO WHAT",
  warning, number_or_list, pov like "POV: ..."), or none.
- payoff_second: the second when the main payoff (crash, joke, reveal) happens.
- has_title_label: a fixed text label/box describing the clip that stays on screen.
- caption_style / caption_position / caption_keyword_color: subtitles of speech, if any.
- layout: how a horizontal game video was made vertical (or full_vertical if it fills the screen).
- facecam: is a streamer's face visible?
- zoom_ins: number of zoom-ins / punch-ins added in editing. cuts: number of hard cuts.
- slow_motion, freeze_frame, replay_of_moment, screen_shake: editing effects used.
- sound_effects: added effects you hear (riser, impact, whoosh, record_scratch, meme_sound, ding),
  or ["none"]. background_music: music added under the video. voiceover: narration added.
- music_type: style of the added music (phonk, hype_edm, hip_hop_rap, epic_orchestral,
  lofi_chill, meme_song, sad_emotional, other) or none. Do not guess the song title.
- cuts_on_beat: do cuts / zooms land on the music's beats?
- emoji_or_stickers: emoji / sticker / meme images popping on screen.
- loop_ending: the last second flows back into the first so it replays seamlessly.
- ends_right_after_payoff: the video ends within ~2 seconds after the payoff.
- pace: overall editing pace. content_type: chaos (gameplay mayhem), reaction (person reacting),
  news, secret (hidden detail), tip (how-to), other.
- topic: 3-6 words, what the Short is about. spoken_language: main spoken language or "none"."""


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


# --- 1. collect ----------------------------------------------------------------------------

def short_score(views, subs):
    """log views, plus up to +0.5 for beating the channel's size (small channels that pop)."""
    s = math.log10(1 + max(views, 0))
    if subs:
        s += 0.5 * math.log10(1 + min(views / subs, 10)) / math.log10(11)
    return s


def label_cohort(rows, cfg):
    """Label rows in place by score percentile within this cohort."""
    ranked = sorted(rows, key=lambda r: r["score"], reverse=True)
    n = len(ranked)
    lo_avg, hi_avg = cfg.get("average_band", [0.3, 0.7])
    for i, r in enumerate(ranked):
        pct = i / max(n - 1, 1)  # 0 = best
        if pct < cfg.get("winner_share", 0.15):
            r["label"] = "winner"
        elif lo_avg <= pct <= hi_avg:
            r["label"] = "average"
        elif pct > 1 - cfg.get("low_share", 0.3):
            r["label"] = "low"
        else:
            r["label"] = "above"
    return ranked


def build_rows(videos, channels, rules, cfg, now):
    sc = rules["scout"]
    subs_of = {}
    for c in channels:
        st = c.get("statistics", {})
        subs_of[c["id"]] = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0) or 0)
    rows = []
    for v in videos:
        sn, st = v.get("snippet", {}), v.get("statistics", {})
        dur = parse_iso_duration(v.get("contentDetails", {}).get("duration"))
        title = sn.get("title", "")
        if not dur or dur > cfg.get("max_duration_s", 180) or not title_ok(title, sc):
            continue
        published = parse_time(sn.get("publishedAt"))
        if not published:
            continue
        age_d = (now - published).total_seconds() / 86400
        views = int(st.get("viewCount", 0) or 0)
        subs = subs_of.get(sn.get("channelId"))
        desc = sn.get("description", "")
        rows.append({
            "research_id": v["id"], "video_id": v["id"], "url": f"https://youtube.com/shorts/{v['id']}",
            "views": views, "age_days": round(age_d, 1), "length_s": dur, "title": title,
            "hashtags": " ".join("#" + h for h in dict.fromkeys(HASHTAG.findall(title + " " + desc))),
            "channel_title": sn.get("channelTitle", ""), "subscribers": "" if subs is None else subs,
            "published_at": sn.get("publishedAt", ""), "collected_at": iso(now),
            "score": round(short_score(views, subs), 3),
            "credits": " ".join("@" + h for h in dict.fromkeys(HANDLE.findall(desc)))[:200],
        })
    return rows


def collect(yt, settings, rules, now):
    cfg = settings["researcher"]
    after = iso(now - timedelta(days=cfg["max_age_days"]))
    before = iso(now - timedelta(days=cfg["min_age_days"]))
    ids, notes = [], []
    for s in cfg["searches"]:
        for order in cfg.get("orders", ["viewCount"]):
            try:
                ids += yt.search_video_ids(s["q"], after, s.get("lang"), order=order,
                                           published_before=before, duration="short")
            except QuotaExceeded as e:
                notes.append(str(e))
                break
            except YouTubeError as e:
                notes.append(f"search '{s['q']}' ({order}) failed: {e}")
    ids = list(dict.fromkeys(ids))
    videos = yt.videos(ids, parts="snippet,statistics,contentDetails") if ids else []
    ch = {v["snippet"]["channelId"] for v in videos}
    channels = yt.channels(ch, parts="statistics") if ch else []
    rows = label_cohort(build_rows(videos, channels, rules, cfg, now), cfg)
    return rows, notes, len(ids)


# --- 1b. collect (fresh tier: 0-72 h) ------------------------------------------------------------

def age_bucket(age_h, edges):
    for i, e in enumerate(edges):
        if age_h <= e:
            return i
    return len(edges)


def label_fresh(rows, fcfg, cfg):
    """fresh_label by score percentile within the same age bucket. Buckets with fewer than
    `min_bucket` Shorts are merged into the next older bucket."""
    edges = fcfg.get("age_buckets_h", [6, 24, 48, 72])
    groups = {}
    for r in rows:
        groups.setdefault(age_bucket(r["age_h"], edges), []).append(r)
    merged, carry = [], []
    for b in sorted(groups):
        carry += groups[b]
        if len(carry) >= fcfg.get("min_bucket", 5):
            merged.append(carry)
            carry = []
    if carry:
        if merged:
            merged[-1] += carry
        else:
            merged.append(carry)
    names = {"winner": "rising", "average": "fresh_average", "low": "fresh_low", "above": "fresh_above"}
    for g in merged:
        tmp = [{"score": r["score"], "_r": r} for r in g]
        label_cohort(tmp, cfg)
        for t in tmp:
            t["_r"]["fresh_label"] = names[t["label"]]
    return rows


def build_fresh_rows(videos, channels, rules, cfg, now):
    """Like build_rows, but for Shorts <= max_age_hours old; score uses views per hour."""
    fcfg = cfg["fresh"]
    out = []
    for r in build_rows(videos, channels, rules, cfg, now):
        age_h = (now - parse_time(r["published_at"])).total_seconds() / 3600
        if age_h > fcfg.get("max_age_hours", 72) or age_h < fcfg.get("min_age_hours", 1):
            continue
        vph = r["views"] / max(age_h, 1.0)
        subs = r["subscribers"] if r["subscribers"] != "" else None
        bonus = short_score(r["views"], subs) - short_score(r["views"], None)
        fresh = {k: r[k] for k in ("research_id", "video_id", "url", "views", "age_days", "length_s",
                                   "title", "hashtags", "channel_title", "subscribers", "published_at",
                                   "credits")}
        fresh.update({"age_h": age_h, "views_per_hour": round(vph, 1), "fresh_at": iso(now),
                      "score": round(math.log10(1 + vph) + bonus, 3)})
        out.append(fresh)
    label_fresh(out, fcfg, cfg)
    for r in out:
        r.pop("age_h")
        r.pop("score")  # the settled score column stays the settled tier's
    return out


def scout_short_ids(candidates, now, max_age_h=72):
    """Shorts Scout already found (free: no search quota)."""
    out = []
    for c in candidates:
        t = parse_time(c.get("published_at"))
        if c.get("kind") == "short" and t and (now - t).total_seconds() <= max_age_h * 3600:
            out.append(c["video_id"])
    return out


def collect_fresh(yt, settings, rules, now, scout_ids=()):
    cfg = settings["researcher"]
    fcfg = cfg["fresh"]
    after = iso(now - timedelta(hours=fcfg.get("max_age_hours", 72)))
    ids, notes = [], []
    for s in fcfg["searches"]:
        try:
            ids += yt.search_video_ids(s["q"], after, s.get("lang"), order=s.get("order", "viewCount"),
                                       duration="short")
        except QuotaExceeded as e:
            notes.append(str(e))
            break
        except YouTubeError as e:
            notes.append(f"fresh search '{s['q']}' failed: {e}")
    from_search = len(set(ids))
    ids = list(dict.fromkeys(list(ids) + list(scout_ids)))
    videos = []
    try:
        videos = yt.videos(ids, parts="snippet,statistics,contentDetails") if ids else []
        ch = {v["snippet"]["channelId"] for v in videos}
        channels = yt.channels(ch, parts="statistics") if ch else []
    except QuotaExceeded as e:
        notes.append(str(e))
        return [], notes, len(ids), from_search
    rows = build_fresh_rows(videos, channels, rules, cfg, now)
    return rows, notes, len(ids), from_search


# --- 2. analyze --------------------------------------------------------------------------------

def pick_to_analyze(rows, per_label):
    """Not-yet-analysed winners and average Shorts, youngest first (just settled),
    alternating winner / average so both groups fill up even if the budget runs out."""
    groups = []
    for label in ("winner", "average"):
        todo = [r for r in rows if r.get("label") == label and not str(r.get("features", "")).strip()]
        todo.sort(key=lambda r: _f(r.get("age_days"), 99))
        groups.append(todo[:per_label])
    out = []
    for i in range(max(len(g) for g in groups)):
        out += [g[i] for g in groups if i < len(g)]
    return out


def pick_fresh(rows, per_label, now, max_age_h=72):
    """Not-yet-analysed rising + fresh_average Shorts seen in the last run, alternating."""
    groups = []
    for label in FRESH_LABELS:
        todo = [r for r in rows if r.get("fresh_label") == label and not str(r.get("features", "")).strip()
                and parse_time(r.get("fresh_at")) and (now - parse_time(r["fresh_at"])).total_seconds()
                <= 12 * 3600 and _f(r.get("age_days"), 99) * 24 <= max_age_h]
        todo.sort(key=lambda r: -_f(r.get("views_per_hour")))
        groups.append(todo[:per_label])
    out = []
    for i in range(max((len(g) for g in groups), default=0)):
        out += [g[i] for g in groups if i < len(g)]
    return out


def flatten(features):
    """A few headline features also go into their own columns for easy reading."""
    return {"hook_type": features.get("hook_type", ""),
            "caption_style": features.get("caption_style", ""),
            "notes": f"{features.get('content_type', '')}: {features.get('topic', '')}"[:120]}


def analyze(gem, rows, now, parallel=4, max_minutes=15, clock=time.monotonic):
    """Returns ({research_id: column updates}, notes)."""
    updates, notes = {}, []
    started, i = clock(), 0
    while i < len(rows):
        if (clock() - started) / 60 >= max_minutes:
            notes.append(f"stopped after {max_minutes} min")
            break
        size = min(parallel, gem.remaining(), len(rows) - i)
        if size <= 0:
            notes.append("daily Gemini budget for the Researcher used up")
            break
        wave, i = rows[i:i + size], i + size

        def ask(r):
            try:
                url = f"https://www.youtube.com/watch?v={r['research_id']}"
                return gem.generate_json([{"fileData": {"fileUri": url}}], PROMPT, SCHEMA,
                                         temperature=0.2), None
            except GeminiError as e:
                return None, e
        with ThreadPoolExecutor(max_workers=size) as pool:
            results = list(pool.map(ask, wave))
        stop = False
        for r, (res, err) in zip(wave, results):
            if err is not None:
                notes.append(f"{r['research_id']}: {err}")
                if isinstance(err, BudgetReached):
                    stop = True
                elif not isinstance(err, Transient):
                    updates[r["research_id"]] = {"features": json.dumps({"error": str(err)[:80]}),
                                                 "analyzed_at": iso(now)}
                continue
            feats, model = res
            updates[r["research_id"]] = {"features": json.dumps(feats, ensure_ascii=False),
                                         "model": model, "analyzed_at": iso(now), **flatten(feats)}
        if stop:
            break
    return updates, notes


# --- 3. findings -------------------------------------------------------------------------------

BOOL_FEATURES = ["text_on_screen_first_second", "has_title_label", "caption_keyword_color", "facecam",
                 "slow_motion", "freeze_frame", "replay_of_moment", "screen_shake", "background_music",
                 "cuts_on_beat",
                 "voiceover", "emoji_or_stickers", "loop_ending", "ends_right_after_payoff"]
NUM_FEATURES = ["payoff_second", "zoom_ins", "cuts", "length_s"]
ENUM_FEATURES = ["hook_text_kind", "hook_type", "caption_style", "caption_position", "layout", "pace", "content_type",
                 "music_type"]
SECTION = {"hook_text_kind": "hook", "text_on_screen_first_second": "hook", "hook_type": "hook", "payoff_second": "hook",
           "has_title_label": "text", "caption_style": "captions", "caption_position": "captions",
           "caption_keyword_color": "captions", "layout": "framing", "facecam": "framing",
           "length_s": "length", "content_type": "content",
           "background_music": "sound", "music_type": "sound", "cuts_on_beat": "sound"}


def features_of(row):
    try:
        f = json.loads(row.get("features") or "{}")
    except ValueError:
        return None
    if not f or "error" in f:
        return None
    f["length_s"] = _f(row.get("length_s"))
    return f


def _share(items, test):
    return sum(1 for x in items if test(x)) / len(items) if items else 0.0


def _verdict(w, a, min_diff):
    if w - a >= min_diff:
        return "do"
    if a - w >= min_diff:
        return "avoid"
    return "same"


def compare(winners, average, min_diff=0.15):
    """Feature-by-feature comparison -> list of finding dicts."""
    out = []
    for k in BOOL_FEATURES:
        ws_, as_ = [f for f in winners if k in f], [f for f in average if k in f]
        if len(ws_) < 3 or len(as_) < 3:
            continue
        w, a = _share(ws_, lambda f: f.get(k) is True), _share(as_, lambda f: f.get(k) is True)
        out.append({"key": k, "section": SECTION.get(k, "editing"), "w": w, "a": a,
                    "verdict": _verdict(w, a, min_diff), "kind": "share"})
    for s in SFX[:-1]:
        test = (lambda s: lambda f: s in (f.get("sound_effects") or []))(s)
        w, a = _share(winners, test), _share(average, test)
        out.append({"key": f"sfx_{s}", "section": "sound", "w": w, "a": a,
                    "verdict": _verdict(w, a, min_diff), "kind": "share"})
    for k in NUM_FEATURES:
        wv = [_f(f.get(k)) for f in winners if f.get(k) is not None]
        av = [_f(f.get(k)) for f in average if f.get(k) is not None]
        if wv and av:
            out.append({"key": k, "section": SECTION.get(k, "editing"), "w": statistics.median(wv),
                        "a": statistics.median(av), "verdict": "info", "kind": "median"})
    for k in ENUM_FEATURES:
        ws_, as_ = [f for f in winners if f.get(k)], [f for f in average if f.get(k)]
        if len(ws_) < 3 or len(as_) < 3:
            continue
        vals = {f.get(k) for f in ws_ + as_}
        for v in sorted(vals):
            test = (lambda v: lambda f: f.get(k) == v)(v)
            w, a = _share(ws_, test), _share(as_, test)
            verdict = _verdict(w, a, min_diff)
            if verdict != "same" or w >= 0.4:
                out.append({"key": f"{k}={v}", "section": SECTION.get(k, "editing"), "w": w, "a": a,
                            "verdict": verdict, "kind": "share"})
    return out


def seo_findings(win_rows, avg_rows, min_diff=0.15):
    def stats(rows):
        titles = [r.get("title", "") for r in rows]
        return {
            "title_chars": statistics.median([len(t) for t in titles]) if titles else 0,
            "emoji_in_title": _share(titles, lambda t: bool(EMOJI.search(t))),
            "question_in_title": _share(titles, lambda t: "?" in t),
            "caps_word_in_title": _share(titles, lambda t: any(w.isupper() and len(w) > 2 and w not in ("GTA",)
                                                              for w in t.split())),
            "hashtags": statistics.median([len(str(r.get("hashtags", "")).split()) for r in rows]) if rows else 0,
        }
    w, a = stats(win_rows), stats(avg_rows)
    out = []
    for k in w:
        median = k in ("title_chars", "hashtags")
        out.append({"key": f"title.{k}", "section": "seo", "w": w[k], "a": a[k],
                    "verdict": "info" if median else _verdict(w[k], a[k], min_diff),
                    "kind": "median" if median else "share"})
    # hashtags and title phrases that winners use much more often
    for name, getter in (("hashtag", lambda r: set(str(r.get("hashtags", "")).lower().split())),
                         ("phrase", lambda r: keywords.phrases(r.get("title", "")))):
        cw, ca = {}, {}
        for r in win_rows:
            for t in getter(r):
                cw[t] = cw.get(t, 0) + 1
        for r in avg_rows:
            for t in getter(r):
                ca[t] = ca.get(t, 0) + 1
        best = sorted(((c / len(win_rows), t) for t, c in cw.items() if c >= 3), reverse=True)
        for share_w, t in best[:5]:
            share_a = ca.get(t, 0) / max(len(avg_rows), 1)
            if share_w >= 1.5 * share_a + 0.05:
                out.append({"key": f"{name}:{t}", "section": "seo", "w": share_w, "a": share_a,
                            "verdict": "do", "kind": "share"})
    return out


def creators_finding(win_rows, top=5):
    count = {}
    for r in win_rows:
        for h in str(r.get("credits", "")).split():
            count[h] = count.get(h, 0) + 1
    return [{"key": f"clipped:{h}", "section": "creators", "w": c, "a": "", "verdict": "info",
             "kind": "count"} for h, c in sorted(count.items(), key=lambda x: -x[1])[:top] if c >= 2]


def topics_today(win_rows, analysed_feats, top=5):
    count = {}
    for r in win_rows:
        f = analysed_feats.get(r["research_id"])
        if f and f.get("topic"):
            t = f["topic"].lower().strip()
            count[t] = count.get(t, 0) + 1
    return [{"key": f"topic:{t}", "section": "today", "w": c, "a": "", "verdict": "info", "kind": "count"}
            for t, c in sorted(count.items(), key=lambda x: -x[1])[:top]]


def hot_words(win_feats, avg_feats, min_count=3, top=8):
    """Topic words that rising Shorts use much more often than average ones -> Scout boost."""
    def words(fs):
        c = {}
        for f in fs:
            for w in set(keywords.tokens(f.get("topic", ""))):
                if w in keywords.STOPWORDS or w in keywords.BASE_WORDS or len(w) < 3 or w.isdigit():
                    continue
                c[w] = c.get(w, 0) + 1
        return c
    cw, ca = words(win_feats), words(avg_feats)
    out = []
    for w, n in sorted(cw.items(), key=lambda x: -x[1]):
        sw, sa = n / max(len(win_feats), 1), ca.get(w, 0) / max(len(avg_feats), 1)
        if n >= min_count and sw >= 1.5 * sa + 0.05:
            out.append({"key": f"word:{w}", "section": "topics", "w": sw, "a": sa, "verdict": "do",
                        "kind": "share"})
    return out[:top]


def describe(f):
    k, w, a = f["key"], f["w"], f["a"]
    if f["kind"] == "share":
        nums = f"winners {w:.0%} vs average {a:.0%}"
    elif f["kind"] == "median":
        nums = f"median: winners {w:g} vs average {a:g}"
    else:
        nums = f"{w} winning Shorts"
    lead = {"do": "DO", "avoid": "AVOID", "same": "no difference", "info": "info"}[f["verdict"]]
    text = f"{lead}: {k.replace('_', ' ')} ({nums})"
    if f.get("shift"):
        text += f" SHIFT (7-day: {f['shift']})"
    return text, nums


def settled_compare(rows, cfg, now):
    """Raw winners-vs-average comparison of the settled tier (for shift detection)."""
    window = timedelta(days=cfg.get("window_days", 7))
    recent = [r for r in rows if parse_time(r.get("analyzed_at")) and now - parse_time(r["analyzed_at"]) <= window]
    fw = [features_of(r) for r in recent if r.get("label") == "winner"]
    fa = [features_of(r) for r in recent if r.get("label") == "average"]
    fw, fa = [f for f in fw if f], [f for f in fa if f]
    if len(fw) < cfg.get("min_group", 8) or len(fa) < cfg.get("min_group", 8):
        return {}
    return {f["key"]: f["verdict"] for f in compare(fw, fa, cfg.get("min_diff", 0.15))}


def findings_now(rows, cfg, now):
    """Rising vs fresh_average Shorts seen in the last `window_hours` -> research.now.* rows."""
    fcfg = cfg["fresh"]
    window = timedelta(hours=fcfg.get("window_hours", 72))
    recent = [r for r in rows if r.get("fresh_label") in FRESH_LABELS and parse_time(r.get("fresh_at"))
              and now - parse_time(r["fresh_at"]) <= window and features_of(r)]
    win = [r for r in recent if r["fresh_label"] == "rising"]
    avg = [r for r in recent if r["fresh_label"] == "fresh_average"]
    need = fcfg.get("min_group", 6)
    if len(win) < need or len(avg) < need:
        return [], f"not enough analysed fresh Shorts yet ({len(win)} rising, {len(avg)} average; need {need} each)"
    fw, fa = [features_of(r) for r in win], [features_of(r) for r in avg]
    min_diff = fcfg.get("min_diff", cfg.get("min_diff", 0.15))
    out = compare(fw, fa, min_diff) + seo_findings(win, avg, min_diff) + hot_words(fw, fa)
    old = settled_compare(rows, cfg, now)
    for f in out:
        prev = old.get(f["key"])
        if prev in ("do", "avoid", "same") and f["verdict"] in ("do", "avoid", "same") \
                and prev != f["verdict"]:
            f["shift"] = {"do": "DO", "avoid": "AVOID", "same": "no difference"}.get(prev, prev)
    src = f"researcher now {now.date().isoformat()} (n={len(win)} rising / {len(avg)} average, last {fcfg.get('window_hours', 72)} h)"
    play = []
    for f in out:
        rule, value = describe(f)
        play.append({"rule_id": f"research.now.{f['section']}.{f['key']}", "section": f"research.now.{f['section']}",
                     "rule": rule, "value": value, "source": src, "updated_at": iso(now)})
    return play, src


def findings(rows, cfg, now):
    window = timedelta(days=cfg.get("window_days", 7))
    recent = [r for r in rows if parse_time(r.get("analyzed_at")) and now - parse_time(r["analyzed_at"]) <= window]
    feats = {r["research_id"]: features_of(r) for r in recent}
    win = [r for r in recent if r.get("label") == "winner" and feats.get(r["research_id"])]
    avg = [r for r in recent if r.get("label") == "average" and feats.get(r["research_id"])]
    if len(win) < cfg.get("min_group", 8) or len(avg) < cfg.get("min_group", 8):
        return [], f"not enough analysed Shorts yet ({len(win)} winners, {len(avg)} average; need " \
                   f"{cfg.get('min_group', 8)} each)"
    fw, fa = [feats[r["research_id"]] for r in win], [feats[r["research_id"]] for r in avg]
    out = compare(fw, fa, cfg.get("min_diff", 0.15))
    out += seo_findings(win, avg, cfg.get("min_diff", 0.15))
    # creators / topics: all labelled winners in the window (not only analysed ones)
    all_win = [r for r in rows if r.get("label") == "winner"
               and parse_time(r.get("collected_at")) and now - parse_time(r["collected_at"]) <= window]
    out += creators_finding(all_win)
    out += topics_today(win, feats)
    src = f"researcher {now.date().isoformat()} (n={len(win)} winners / {len(avg)} average, last {window.days} d)"
    play = []
    for f in out:
        rule, value = describe(f)
        play.append({"rule_id": f"research.{f['section']}.{f['key']}", "section": f"research.{f['section']}",
                     "rule": rule, "value": value, "source": src, "updated_at": iso(now)})
    return play, src


def style_rows(rows, settings, now):
    """style.* Playbook rows: the clip style winners use (7-day winners + rising fresh Shorts)."""
    cfg, fcfg = settings["researcher"], settings["researcher"]["fresh"]
    week, fresh_win = timedelta(days=cfg.get("window_days", 7)), timedelta(hours=fcfg.get("window_hours", 72))
    win, avg, seen = [], [], set()
    for r in rows:
        f = features_of(r)
        if not f or r.get("research_id") in seen:
            continue
        t, ft = parse_time(r.get("analyzed_at")), parse_time(r.get("fresh_at"))
        recent = t and now - t <= week
        fresh = ft and now - ft <= fresh_win
        if (recent and r.get("label") == "winner") or (fresh and r.get("fresh_label") == "rising"):
            win.append(f)
        elif (recent and r.get("label") == "average") or (fresh and r.get("fresh_label") == "fresh_average"):
            avg.append(f)
        else:
            continue
        seen.add(r.get("research_id"))
    defaults = settings["render"].get("style_defaults", {})
    st, why, ex = clip_style.choose(win, avg, defaults, cfg.get("min_group", 8), cfg.get("min_diff", 0.15))
    src = f"researcher style {now.date().isoformat()} (n={len(win)} winners / {len(avg)} average)"
    return clip_style.playbook_rows(st, why, ex, src, iso(now))


# --- run ----------------------------------------------------------------------------------------

def main(argv=None):
    what = (argv or sys.argv[1:] or ["all"])[0]
    settled = what in ("collect", "analyze", "findings", "all")
    fresh = what in ("fresh", "all")
    step = {"collect": "collect", "analyze": "analyze", "findings": "findings"}.get(what)
    settings, rules = load_settings(), load_rules()
    cfg = settings["researcher"]
    fcfg = cfg["fresh"]
    now = datetime.now(timezone.utc)
    try:
        ss = sheets.open_sheet(sheets.get_client(), settings["sheet"]["id"])
        for tab in (TAB, "Playbook"):
            sheets.ensure_tab(ss, tab, TABS[tab])
        ws = ss.worksheet(TAB)
    except sheets.SheetSetupError as e:
        print(f"RESEARCHER FAILED (sheet): {e}")
        return 1

    if settled and step in (None, "collect"):
        try:
            yt = YouTube(budget_units=cfg["run_budget_units"])
            rows, notes, n = collect(yt, settings, rules, now)
        except YouTubeError as e:
            print(f"RESEARCHER FAILED (YouTube): {e}")
            return 1
        for note in notes:
            print(f"note: {note}")
        upd, new = sheets.upsert_rows(ws, TABS[TAB], "research_id", rows)
        by = {}
        for r in rows:
            by[r["label"]] = by.get(r["label"], 0) + 1
        print(f"Collected {n} Shorts, {len(rows)} GTA 6 Shorts 3-{cfg['max_age_days']} days old: {by}. "
              f"Research: {new} new, {upd} updated. Quota used: {yt.used}/{yt.budget} units.")

    if fresh:
        scout_ids = []
        try:
            scout_ids = scout_short_ids(sheets.read_records(ss.worksheet("Candidates")), now,
                                        fcfg.get("max_age_hours", 72))
        except Exception as e:  # Candidates tab missing / unreadable: search only
            print(f"note: Scout Shorts not read: {e}")
        try:
            yt = YouTube(budget_units=fcfg["run_budget_units"])
            rows, notes, n, n_search = collect_fresh(yt, settings, rules, now, scout_ids)
        except YouTubeError as e:
            print(f"RESEARCHER FAILED (YouTube, fresh): {e}")
            return 1
        for note in notes:
            print(f"note: {note}")
        upd, new = sheets.upsert_rows(ws, TABS[TAB], "research_id", rows)
        by = {}
        for r in rows:
            by[r["fresh_label"]] = by.get(r["fresh_label"], 0) + 1
        print(f"Fresh: {n} Shorts ({n_search} from search, {len(scout_ids)} from Scout), {len(rows)} GTA 6 "
              f"Shorts 0-{fcfg.get('max_age_hours', 72)} h old: {by}. Research: {new} new, {upd} updated. "
              f"Quota used: {yt.used}/{yt.budget} units.")

    if step in (None, "analyze"):
        gcfg = settings["gemini"]["researcher"]
        try:
            gem = Gemini(gcfg["models"], gcfg["daily_budget"], "researcher", usage=Usage(USAGE_FILE),
                         retry_wait=rules["watcher"].get("retry_wait_seconds", 45),
                         requests_per_minute=gcfg.get("requests_per_minute"))
        except GeminiError as e:
            print(f"RESEARCHER FAILED: {e}")
            return 1
        records = sheets.read_records(ws)
        todo = []
        if fresh:
            todo += pick_fresh(records, fcfg["analyze_per_label"], now, fcfg.get("max_age_hours", 72))
        if settled:
            seen = {r["research_id"] for r in todo}
            todo += [r for r in pick_to_analyze(records, cfg["analyze_per_label"]) if r["research_id"] not in seen]
        print(f"Analysing {len(todo)} Shorts blind with Gemini (calls left today: {gem.remaining()}).")
        updates, notes = analyze(gem, todo, now, gcfg.get("parallel_calls", 4), gcfg.get("max_run_minutes", 15))
        for note in notes:
            print(f"  {note}")
        sheets.set_values(ws, TABS[TAB], "research_id", updates)
        print(f"Analysed {sum(1 for u in updates.values() if 'model' in u)}; Gemini calls this run: "
              f"{gem.calls_this_run}.")

    if step in (None, "findings"):
        records = sheets.read_records(ws)
        play, msgs = [], []
        if settled:
            p, src = findings(records, cfg, now)
            play += p
            msgs.append(f"7-day: {src}")
        if fresh:
            p, src = findings_now(records, cfg, now)
            play += p
            msgs.append(f"right now: {src}")
        for m in msgs:
            print(f"Findings {m}.")
        sp = style_rows(records, settings, now)
        if sp:
            play += sp
            print("Style (what winners do):")
            for p in sp:
                print(f"  {p['rule'][:160]}")
        if play:
            sheets.upsert_rows(ss.worksheet("Playbook"), TABS["Playbook"], "rule_id", play)
            for p in play:
                if p["rule"].startswith(("DO", "AVOID")) or "SHIFT" in p["rule"] or \
                        p["section"] in ("research.today", "research.length"):
                    print(f"  [{p['section']}] {p['rule']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
