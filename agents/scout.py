"""Scout: find trending GTA 6 videos, live-stream VODs and Shorts -> `Candidates` tab.

One run:
  1. search.list for every query in settings.youtube.searches (EN + Hindi + finished
     live streams), last `max_age_hours`
  2. recent uploads of watchlist creators
  3. videos.list + channels.list for numbers (cheap, 1 unit per 50)
  4. classify (short / long / live_vod / live_now), filter (GTA 6 only, blocklist,
     leaks marked blocked_leak), score, campaign lead scan
  5. upsert into Candidates (no duplicates; found_at and status are kept)

Run:  python -m agents.scout      (needs YOUTUBE_API_KEY, GOOGLE_SERVICE_ACCOUNT_JSON)
"""
import math
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

from lib import keywords, sheets
from lib.config import load_blocklist, load_rules, load_settings, load_watchlist
from lib.schema import TABS
from lib.youtube import QuotaExceeded, YouTube, YouTubeError

TAB = "Candidates"
URL_RE = re.compile(r"https?://[^\s<>\"')]+|(?:discord\.gg|whop\.com)/[^\s<>\"')]+", re.I)
DUR_RE = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


# --- pure helpers (unit-tested) ----------------------------------------------------

def parse_iso_duration(s):
    m = DUR_RE.fullmatch(s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify(video, short_max_seconds):
    live = video.get("liveStreamingDetails")
    if live:
        return "live_vod" if live.get("actualEndTime") else "live_now"
    secs = parse_iso_duration(video.get("contentDetails", {}).get("duration"))
    return "short" if 0 < secs <= short_max_seconds else "long"


HASHTAG_RE = re.compile(r"#\S+")


def clean_title(title):
    """Title without #hashtags (people tag GTA 5 videos with #gta6 for reach)."""
    return HASHTAG_RE.sub(" ", title or "")


def title_is_gta6(title, pattern):
    return re.search(pattern, clean_title(title), re.I) is not None


def is_gta6(video, pattern):
    return title_is_gta6(video.get("snippet", {}).get("title", ""), pattern)


def script_ok(title, allowed=("LATIN", "DEVANAGARI"), max_foreign_share=0.3):
    """True if the title is mostly English/Hindi letters (we can't caption other languages)."""
    letters = [ch for ch in clean_title(title) if ch.isalpha()]
    if not letters:
        return True
    foreign = sum(1 for ch in letters if not unicodedata.name(ch, "").startswith(tuple(allowed)))
    return foreign / len(letters) <= max_foreign_share


WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def looks_foreign(title, sc):
    """Spanish/Portuguese titles use Latin letters too, so check telltale characters and
    common words (2+ hits). 'Los Santos' is GTA's city, so 'los' is not on the list."""
    t = clean_title(title).lower()
    if any(ch in t for ch in sc.get("foreign_chars", "")):
        return True
    words = set(WORD_RE.findall(t))
    return len(words & set(sc.get("foreign_words", []))) >= sc.get("foreign_words_min", 2)


def language_ok(video, sc):
    """Use YouTube's declared language when the creator set one."""
    sn = video.get("snippet", {})
    lang = (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").lower()
    return not lang or lang.split("-")[0] in sc.get("allowed_languages", ["en", "hi"])


def title_ok(title, sc):
    return title_is_gta6(title, sc["gta6_pattern"]) and script_ok(
        title, tuple(sc["allowed_scripts"]), sc["max_foreign_letter_share"]) \
        and not looks_foreign(title, sc)


def is_leak(video, leak_words):
    title = video.get("snippet", {}).get("title", "").lower()
    return any(re.search(r"\b" + re.escape(w) + r"\b", title) for w in leak_words)


def campaign_scan(texts, cfg):
    """Return (is_lead, link). A link to a known campaign domain wins over keywords."""
    joined = "\n".join(t for t in texts if t)
    low = joined.lower()
    for url in URL_RE.findall(joined):
        if any(dom in url.lower() for dom in cfg["link_domains"]):
            return True, url.rstrip(".,")
    if any(kw in low for kw in cfg["keywords"]):
        return True, ""
    return False, ""


def score(views_per_hour, outlier, cfg, watchlist=False, campaign=False, hot_topic=False):
    o = min(outlier or 0, cfg["outlier_cap"])
    s = views_per_hour * (1 + math.log10(1 + o))
    if watchlist:
        s *= cfg["watchlist_boost"]
    if campaign:
        s *= cfg["campaign_boost"]
    if hot_topic:
        s *= cfg.get("topic_boost", 1.3)
    return s


def hot_topic_words(playbook_rows, now, max_age_h=36):
    """Topic words of Shorts rising right now (Researcher, Playbook research.now.topics)."""
    out = set()
    for r in playbook_rows:
        rid = str(r.get("rule_id", ""))
        if not rid.startswith("research.now.topics.word:"):
            continue
        t = parse_time(r.get("updated_at")) if r.get("updated_at") else None
        if t and (now - t).total_seconds() <= max_age_h * 3600:
            out.add(rid.split("word:", 1)[1].lower())
    return out


def matches_topic(title, words):
    return bool(words) and bool(set(keywords.tokens(title)) & words)


def build_candidates(videos, channels, found_by, watch_ids, block_ids, rules, now, hot_words=frozenset()):
    """Turn API items into Candidates records (dicts), best first."""
    sc = rules["scout"]
    ch_by_id = {c["id"]: c for c in channels}
    out = []
    for v in videos:
        sn, st = v.get("snippet", {}), v.get("statistics", {})
        cid = sn.get("channelId", "")
        if cid in block_ids or not title_ok(sn.get("title", ""), sc) or not language_ok(v, sc):
            continue
        published = parse_time(sn["publishedAt"])
        age_h = max((now - published).total_seconds() / 3600, 0.0)
        in_watch = cid in watch_ids
        if age_h > sc["max_age_hours"] and not in_watch:
            continue
        views = int(st.get("viewCount", 0))
        if views < sc["min_views"] and not in_watch:
            continue
        ch = ch_by_id.get(cid, {})
        cst = ch.get("statistics", {})
        subs = None if cst.get("hiddenSubscriberCount") else int(cst.get("subscriberCount", 0) or 0)
        outlier = views / subs if subs else None
        vph = views / max(age_h, 1.0)
        lead, link = campaign_scan(
            [sn.get("description", ""), ch.get("snippet", {}).get("description", "")],
            rules["campaign_lead"],
        )
        kind = classify(v, sc["short_max_seconds"])
        status = "blocked_leak" if is_leak(v, sc["leak_words"]) else (
            "live_now" if kind == "live_now" else "new")
        out.append({
            "video_id": v["id"],
            "url": ("https://youtube.com/shorts/" if kind == "short" else "https://youtu.be/") + v["id"],
            "title": sn.get("title", ""),
            "channel_id": cid,
            "channel_title": sn.get("channelTitle", ""),
            "channel_handle": handle_of(ch),
            "published_at": sn["publishedAt"],
            "kind": kind,
            "duration_s": parse_iso_duration(v.get("contentDetails", {}).get("duration")),
            "views": views,
            "subscribers": "" if subs is None else subs,
            "age_h": round(age_h, 1),
            "views_per_hour": round(vph, 1),
            "outlier_score": "" if outlier is None else round(outlier, 3),
            "is_watchlist": in_watch,
            "campaign_lead": lead,
            "campaign_link": link,
            "found_at": iso(now),
            "status": status,
            "score": round(score(vph, outlier, sc, in_watch, lead,
                                 matches_topic(sn.get("title", ""), hot_words)), 1),
            "found_by": found_by.get(v["id"], "") + (" +hot topic" if matches_topic(sn.get("title", ""), hot_words) else ""),
            "updated_at": iso(now),
        })
    out.sort(key=lambda r: r["score"], reverse=True)
    return out


# --- run ---------------------------------------------------------------------------

def collect(yt, settings, rules, watchlist, now):
    """Search + watchlist. Returns (video_ids in order, found_by map, notes)."""
    since = iso(now - timedelta(hours=rules["scout"]["max_age_hours"]))
    found_by, notes = {}, []
    for s in settings["youtube"]["searches"]:
        label = s["q"] + (f" [{s['event_type']}]" if s.get("event_type") else "") + f" ({s.get('lang', '')})"
        try:
            ids = yt.search_video_ids(s["q"], since, s.get("lang"), s.get("event_type"))
        except QuotaExceeded as e:
            notes.append(str(e))
            break
        except YouTubeError as e:
            notes.append(f"search '{label}' failed: {e}")
            continue
        for vid in ids:
            found_by.setdefault(vid, label)
    for c in watchlist.get("creators", []):
        try:
            for vid in yt.recent_uploads(c["channel_id"], 5):
                found_by.setdefault(vid, "watchlist")
        except QuotaExceeded as e:
            notes.append(str(e))
            break
        except YouTubeError as e:
            notes.append(f"watchlist {c.get('name', c['channel_id'])} failed: {e}")
    return list(found_by), found_by, notes


def handle_of(channel):
    """'@handle' from a channels.list item (snippet.customUrl), or '' if the channel has none."""
    h = str(channel.get("snippet", {}).get("customUrl") or "").strip()
    if not h:
        return ""
    return h if h.startswith("@") else "@" + h


def run(yt, settings, rules, watchlist, blocklist, now=None, hot_words=frozenset()):
    now = now or datetime.now(timezone.utc)
    ids, found_by, notes = collect(yt, settings, rules, watchlist, now)
    videos = yt.videos(ids) if ids else []
    ch_ids = {v["snippet"]["channelId"] for v in videos}
    channels = yt.channels(ch_ids) if ch_ids else []
    watch_ids = {c["channel_id"] for c in watchlist.get("creators", [])}
    block_ids = {c if isinstance(c, str) else c.get("channel_id") for c in blocklist.get("channels", [])}
    records = build_candidates(videos, channels, found_by, watch_ids, block_ids, rules, now, hot_words)
    return records, notes, len(ids)


def update_keywords(ss, records, settings, now):
    """Mine trending phrases from this run's titles -> Keywords tab (trend vs last run)."""
    sheets.ensure_tab(ss, "Keywords", TABS["Keywords"])
    ws = ss.worksheet("Keywords")
    old = {r["phrase"]: r for r in sheets.read_records(ws)}
    rows = keywords.mine([r for r in records if r.get("status") != "blocked_leak"],
                         top=settings.get("keywords", {}).get("top", 40))
    for r in rows:
        prev = old.get(r["phrase"], {})
        r["prev_score"] = prev.get("score", "")
        r["trend"] = keywords.trend(r["score"], prev.get("score"))
        r["first_seen"] = prev.get("first_seen") or iso(now)
        r["updated_at"] = iso(now)
    sheets.upsert_rows(ws, TABS["Keywords"], "phrase", rows, preserve=("first_seen",))
    return rows


def read_hot_words(settings):
    """Best effort: the Researcher's rising topic words (empty if the sheet can't be read)."""
    try:
        ss = sheets.open_sheet(sheets.get_client(), settings["sheet"]["id"])
        return hot_topic_words(sheets.read_records(ss.worksheet("Playbook")), datetime.now(timezone.utc))
    except Exception as e:  # never block Scout on this
        print(f"note: hot topics not read: {e}")
        return set()


def main():
    settings, rules = load_settings(), load_rules()
    hot = read_hot_words(settings)
    if hot:
        print(f"Hot topic words (Researcher, right now): {', '.join(sorted(hot))}")
    try:
        yt = YouTube(budget_units=settings["youtube"]["scout_run_budget_units"])
        records, notes, n_found = run(yt, settings, rules, load_watchlist(), load_blocklist(), hot_words=hot)
    except YouTubeError as e:
        print(f"SCOUT FAILED (YouTube): {e}")
        return 1
    for n in notes:
        print(f"note: {n}")
    keep = records[: rules["scout"]["max_new_per_run"]]
    print(f"Found {n_found} videos, {len(records)} passed filters, writing top {len(keep)}.")

    try:
        client = sheets.get_client()
        ss = sheets.open_sheet(client, settings["sheet"]["id"])
        sheets.ensure_tab(ss, TAB, TABS[TAB])
        ws = ss.worksheet(TAB)
        upd, new = sheets.upsert_rows(ws, TABS[TAB], "video_id", keep,
                                      preserve=("found_at", "status"))
    except sheets.SheetSetupError as e:
        print(f"SCOUT FAILED (sheet): {e}")
        return 1

    try:
        kw = update_keywords(ss, records, settings, now=datetime.now(timezone.utc))
        print(f"Keywords: top {len(kw)} phrases; rising: "
              + (", ".join(r["phrase"] for r in kw if r["trend"] in ("rising", "new"))[:300] or "none"))
    except sheets.SheetSetupError as e:
        print(f"note: keywords not written: {e}")

    leads = sum(1 for r in keep if r["campaign_lead"])
    print(f"Candidates: {new} new, {upd} updated, {leads} campaign leads.")
    for r in keep[:5]:
        print(f"  top: {r['score']:>10} | {r['kind']:<8} | {r['views_per_hour']:>8} v/h | {r['title'][:60]}")
    print(f"Quota used this run: {yt.used}/{yt.budget} units.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
