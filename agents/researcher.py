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

Run:  python -m agents.researcher [collect|analyze|findings|all]   (default all)
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
from lib.config import REPO_ROOT, load_rules, load_settings
from lib.gemini import BudgetReached, Gemini, GeminiError, Transient, Usage
from lib.schema import TABS
from lib.youtube import QuotaExceeded, YouTube, YouTubeError

TAB = "Research"
USAGE_FILE = REPO_ROOT / "data" / "gemini_usage_researcher.json"
CONTENT_TYPES = ["chaos", "reaction", "news", "secret", "tip", "other"]
HOOKS = ["action_first", "text_question", "text_statement", "face_reaction", "voice_hook",
         "countdown_or_list", "before_after", "none"]
SFX = ["riser", "impact", "whoosh", "record_scratch", "meme_sound", "ding", "none"]
HASHTAG = re.compile(r"#(\w+)", re.UNICODE)
HANDLE = re.compile(r"(?<![\w.])@([A-Za-z0-9_.\-]{3,30})")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "hook_type": {"type": "STRING", "enum": HOOKS},
        "text_on_screen_first_second": {"type": "BOOLEAN"},
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
                 "loop_ending", "pace", "content_type", "topic"],
}

PROMPT = """You are an editing analyst. Watch this YouTube Short about GTA 6 and describe HOW it is
edited, exactly as you see and hear it. Do not judge whether it is good.
- hook_type: what grabs attention in the first 2 seconds (action_first = the action is already
  happening; text_question / text_statement = big on-screen text; face_reaction = a face
  reacting; voice_hook = someone speaking a hook; countdown_or_list; before_after; none).
- text_on_screen_first_second: is there readable on-screen text in the first second?
- payoff_second: the second when the main payoff (crash, joke, reveal) happens.
- has_title_label: a fixed text label/box describing the clip that stays on screen.
- caption_style / caption_position / caption_keyword_color: subtitles of speech, if any.
- layout: how a horizontal game video was made vertical (or full_vertical if it fills the screen).
- facecam: is a streamer's face visible?
- zoom_ins: number of zoom-ins / punch-ins added in editing. cuts: number of hard cuts.
- slow_motion, freeze_frame, replay_of_moment, screen_shake: editing effects used.
- sound_effects: added effects you hear (riser, impact, whoosh, record_scratch, meme_sound, ding),
  or ["none"]. background_music: music added under the video. voiceover: narration added.
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


# --- 2. analyze --------------------------------------------------------------------------------

def pick_to_analyze(rows, per_label):
    """Not-yet-analysed winners and average Shorts, youngest first (just settled)."""
    out = []
    for label in ("winner", "average"):
        todo = [r for r in rows if r.get("label") == label and not str(r.get("features", "")).strip()]
        todo.sort(key=lambda r: _f(r.get("age_days"), 99))
        out += todo[:per_label]
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
                 "voiceover", "emoji_or_stickers", "loop_ending", "ends_right_after_payoff"]
NUM_FEATURES = ["payoff_second", "zoom_ins", "cuts", "length_s"]
ENUM_FEATURES = ["hook_type", "caption_style", "caption_position", "layout", "pace", "content_type"]
SECTION = {"text_on_screen_first_second": "hook", "hook_type": "hook", "payoff_second": "hook",
           "has_title_label": "text", "caption_style": "captions", "caption_position": "captions",
           "caption_keyword_color": "captions", "layout": "framing", "facecam": "framing",
           "length_s": "length", "content_type": "content"}


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
        w, a = _share(winners, lambda f: f.get(k) is True), _share(average, lambda f: f.get(k) is True)
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
        vals = {f.get(k) for f in winners + average if f.get(k)}
        for v in sorted(vals):
            test = (lambda v: lambda f: f.get(k) == v)(v)
            w, a = _share(winners, test), _share(average, test)
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


def describe(f):
    k, w, a = f["key"], f["w"], f["a"]
    if f["kind"] == "share":
        nums = f"winners {w:.0%} vs average {a:.0%}"
    elif f["kind"] == "median":
        nums = f"median: winners {w:g} vs average {a:g}"
    else:
        nums = f"{w} winning Shorts"
    lead = {"do": "DO", "avoid": "AVOID", "same": "no difference", "info": "info"}[f["verdict"]]
    return f"{lead}: {k.replace('_', ' ')} ({nums})", nums


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


# --- run ----------------------------------------------------------------------------------------

def main(argv=None):
    what = (argv or sys.argv[1:] or ["all"])[0]
    settings, rules = load_settings(), load_rules()
    cfg = settings["researcher"]
    now = datetime.now(timezone.utc)
    try:
        ss = sheets.open_sheet(sheets.get_client(), settings["sheet"]["id"])
        for tab in (TAB, "Playbook"):
            sheets.ensure_tab(ss, tab, TABS[tab])
        ws = ss.worksheet(TAB)
    except sheets.SheetSetupError as e:
        print(f"RESEARCHER FAILED (sheet): {e}")
        return 1

    if what in ("collect", "all"):
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

    if what in ("analyze", "all"):
        gcfg = settings["gemini"]["researcher"]
        try:
            gem = Gemini(gcfg["models"], gcfg["daily_budget"], "researcher",
                         usage=Usage(USAGE_FILE), retry_wait=rules["watcher"].get("retry_wait_seconds", 20))
        except GeminiError as e:
            print(f"RESEARCHER FAILED: {e}")
            return 1
        todo = pick_to_analyze(sheets.read_records(ws), cfg["analyze_per_label"])
        print(f"Analysing {len(todo)} Shorts blind with Gemini (calls left today: {gem.remaining()}).")
        updates, notes = analyze(gem, todo, now, gcfg.get("parallel_calls", 4), gcfg.get("max_run_minutes", 15))
        for note in notes:
            print(f"  {note}")
        sheets.set_values(ws, TABS[TAB], "research_id", updates)
        print(f"Analysed {sum(1 for u in updates.values() if 'model' in u)}; Gemini calls this run: "
              f"{gem.calls_this_run}.")

    if what in ("findings", "all"):
        play, src = findings(sheets.read_records(ws), cfg, now)
        if not play:
            print(f"Findings: {src}.")
            return 0
        pws = ss.worksheet("Playbook")
        sheets.upsert_rows(pws, TABS["Playbook"], "rule_id", play)
        print(f"Findings ({src}):")
        for p in play:
            if p["rule"].startswith(("DO", "AVOID")) or p["section"] in ("research.today", "research.length"):
                print(f"  {p['rule']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
