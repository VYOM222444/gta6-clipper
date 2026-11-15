"""Watcher: Gemini checks the most promising windows and returns exact, clip-ready moments.

Jobs, best first:
  1. videos with Signals windows (Moments rows source=signal, gate_status=pending):
     one Gemini call per video, up to `watcher.max_windows_per_call` windows
  2. long videos up to `watcher.whole_video_max_seconds` with no signals: watched whole
Each call returns moments (exact start/end, what happens, content type, quality 1-10,
standalone?, promo?, hook idea). A basic gate marks each one ready or rejected_*.
Output -> `Moments` (source=watcher). Signal rows become gate_status=watched and the
candidate's status becomes watched. Daily call budget: config/settings.json -> gemini.watcher.

Run:  python -m agents.watcher   (needs GEMINI_API_KEY, GOOGLE_SERVICE_ACCOUNT_JSON)
"""
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from agents.scout import title_ok
from lib import sheets
from lib.config import load_blocklist, load_rules, load_settings
from lib.gemini import BudgetReached, Gemini, GeminiError, Transient, video_parts
from lib.schema import TABS

TAB = "Moments"
CONTENT_TYPES = ["chaos", "reaction", "news", "secret", "tip"]
SKIP_STATUS = {"blocked_leak", "blocked", "watched", "watch_failed"}

SCHEMA = {
    "type": "OBJECT",
    "properties": {"moments": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {
            "segment": {"type": "INTEGER"},
            "start": {"type": "NUMBER"},
            "end": {"type": "NUMBER"},
            "what_happens": {"type": "STRING"},
            "content_type": {"type": "STRING", "enum": CONTENT_TYPES},
            "style_tags": {"type": "ARRAY", "items": {"type": "STRING"}},
            "quality": {"type": "INTEGER"},
            "standalone": {"type": "BOOLEAN"},
            "has_promo": {"type": "BOOLEAN"},
            "hook_idea": {"type": "STRING"},
        },
        "required": ["segment", "start", "end", "what_happens", "content_type",
                     "quality", "standalone", "has_promo"],
    }}},
    "required": ["moments"],
}


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def fmt_time(s):
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


# --- choosing work -------------------------------------------------------------------------

def pick_jobs(candidates, moments, rules):
    w, sc = rules["watcher"], rules["scout"]
    cand = {}
    for c in candidates:
        cand.setdefault(str(c.get("video_id", "")), c)

    by_video = {}
    for m in moments:
        if m.get("source") == "signal" and m.get("gate_status") == "pending":
            by_video.setdefault(str(m["video_id"]), []).append(m)

    jobs = []
    for vid, rows in by_video.items():
        c = cand.get(vid)
        if not c or c.get("status") in {"blocked_leak", "blocked"} or not title_ok(c.get("title", ""), sc):
            continue
        rows.sort(key=lambda r: _f(r.get("strength")), reverse=True)
        rows = rows[: w["max_windows_per_call"]]
        prio = _f(rows[0].get("strength")) * (1 + math.log10(1 + _f(c.get("score"))))
        jobs.append({"candidate": c, "signal_rows": rows, "priority": prio,
                     "windows": sorted((_f(r["start_s"]), _f(r["end_s"])) for r in rows)})
    jobs.sort(key=lambda j: j["priority"], reverse=True)

    whole = []
    for vid, c in cand.items():
        if vid in by_video or c.get("status") in SKIP_STATUS:
            continue
        if c.get("kind") != "long" or not (0 < _f(c.get("duration_s")) <= w["whole_video_max_seconds"]):
            continue
        if not title_ok(c.get("title", ""), sc):
            continue
        whole.append({"candidate": c, "signal_rows": [], "windows": [],
                      "priority": _f(c.get("score"))})
    whole.sort(key=lambda j: j["priority"], reverse=True)
    return jobs + whole


# --- prompt + parsing ---------------------------------------------------------------------------

def build_prompt(c, windows, rules):
    w, hook = rules["watcher"], rules["hook"]
    if windows:
        segs = "\n".join(f"  segment {i}: {fmt_time(s)}-{fmt_time(e)} of the original video "
                         f"({int(e - s)} s long)" for i, (s, e) in enumerate(windows))
        where = (f"You are shown {len(windows)} short segments of this video, in order:\n{segs}\n"
                 "For each moment give `segment` (its number) and `start`/`end` in seconds "
                 "counted from the START OF THAT SEGMENT.")
    else:
        where = ("You are shown the whole video. Use `segment` 0 and give `start`/`end` in "
                 "seconds from the start of the video.")
    return f"""You pick moments from GTA 6 videos to turn into vertical Shorts (YouTube Shorts / Instagram Reels).
Video title: "{c.get('title', '')}" by {c.get('channel_title', '')}.

{where}

Return up to 5 of the best clip-worthy moments, best first. A good moment:
- stands on its own with no context from the rest of the video (`standalone`)
- has its payoff (funny, shocking, impressive, hype) within the first {hook['payoff_by_second']} seconds of the clip
- is {w['clip_min_seconds']}-{w['clip_max_seconds']} seconds long; start right before the action, end right after the payoff
- shows clear emotion: laughter, shock, rage, hype or "wow"
Set `has_promo` true if the moment contains a sponsor read, ad, merch or channel promo.
`quality` 1-10: how likely this is to go viral as a Short (be strict; 8+ only for genuinely great moments).
`content_type`: chaos (gameplay mayhem), reaction (streamer reacting), news (announcements/updates), secret (hidden detail/easter egg), tip (how-to).
`style_tags`: 1-4 short tags like "police chase", "funny fail", "streamer scream", "car crash".
`hook_idea`: on-screen hook text, max {hook['max_words']} words, works with sound off.
`what_happens`: one plain sentence.
If nothing is worth clipping, return an empty list."""


def to_absolute(m, windows):
    """Convert a returned moment to absolute (start, end) seconds in the original video."""
    s, e = _f(m.get("start")), _f(m.get("end"))
    if not windows:
        return s, e
    i = int(m.get("segment", 0))
    if not 0 <= i < len(windows):
        return None
    ws, we = windows[i]
    seg_len = we - ws
    if s > seg_len + 5 and ws - 5 <= s <= we + 5:  # model answered in absolute time
        return s, e
    return ws + s, ws + e


def fit_length(start, end, cfg):
    start = max(start, 0)
    if end - start > cfg["clip_max_seconds"]:
        end = start + cfg["clip_max_seconds"]
    if end - start < cfg["clip_min_seconds"]:
        end = start + cfg["clip_min_seconds"]
    return int(start), int(end)


def used_spans(batch_rows, posted_rows, moments):
    """(video_id, start) of moments already in Batch or Posted."""
    spans = {(str(b.get("video_id")), _f(b.get("start_s"))) for b in batch_rows if b.get("video_id")}
    by_id = {str(m.get("moment_id")): m for m in moments}
    for p in posted_rows:
        m = by_id.get(str(p.get("moment_id")))
        if m:
            spans.add((str(m["video_id"]), _f(m.get("start_s"))))
    return spans


def gate(m, vid, start, channel_id, used, block_ids, cfg):
    if channel_id in block_ids:
        return "rejected_blocklist"
    if m.get("has_promo"):
        return "rejected_promo"
    if not m.get("standalone", True):
        return "rejected_context"
    if int(m.get("quality", 0)) < cfg["min_quality"]:
        return "rejected_low"
    if any(v == vid and abs(s - start) <= cfg["overlap_seconds"] for v, s in used):
        return "rejected_used"
    return "ready"


def moments_from_answer(answer, job, used, block_ids, rules, now):
    c, windows, cfg = job["candidate"], job["windows"], rules["watcher"]
    vid = str(c["video_id"])
    signal = "+".join(sorted({p for r in job["signal_rows"] for p in str(r.get("signal", "")).split("+") if p})) or "whole"
    rows, seen = [], set()
    for m in answer.get("moments", [])[:5]:
        span = to_absolute(m, windows)
        if not span:
            continue
        start, end = fit_length(*span, cfg)
        if start in seen:
            continue
        seen.add(start)
        status = gate(m, vid, start, c.get("channel_id", ""), used, block_ids, cfg)
        if status == "ready":
            used.add((vid, start))  # no two ready moments on top of each other
        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append({
            "moment_id": f"{vid}-g{start}", "video_id": vid, "start_s": start, "end_s": end,
            "source": "watcher", "signal": signal, "strength": int(m.get("quality", 0)),
            "reason": m.get("what_happens", ""), "content_type": m.get("content_type", ""),
            "style_tags": ", ".join(m.get("style_tags") or []), "suggested_brand": "",
            "has_promo": bool(m.get("has_promo")), "gate_status": status, "created_at": ts,
            "quality": int(m.get("quality", 0)), "standalone": bool(m.get("standalone", True)),
            "hook_idea": m.get("hook_idea", ""), "title": c.get("title", ""),
            "channel_title": c.get("channel_title", ""),
            "url": f"https://youtu.be/{vid}?t={start}", "watched_at": ts,
        })
    return rows


# --- run ----------------------------------------------------------------------------------------

def _ask(gem, job, rules):
    c = job["candidate"]
    vid = str(c["video_id"])
    what = f"{len(job['windows'])} windows" if job["windows"] else f"whole {int(_f(c.get('duration_s')))} s"
    print(f"  -> asking Gemini about {vid} ({what}) | {c.get('title', '')[:50]}", flush=True)
    url = f"https://www.youtube.com/watch?v={vid}"
    try:
        return gem.generate_json(video_parts(url, job["windows"]),
                                 build_prompt(c, job["windows"], rules), SCHEMA), None
    except GeminiError as e:
        return None, e


def run(gem, jobs, used, block_ids, rules, per_run_cap, now, max_minutes=None, clock=time.monotonic):
    """Ask Gemini about jobs in parallel waves (free tier allows a few requests per minute).
    Returns (moment_rows, watched_signal_ids, watched_video_ids, notes)."""
    out, watched_sig, watched_vid, notes = [], {}, {}, []
    parallel = max(int(rules["watcher"].get("parallel_calls", 1)), 1)
    started, i, sent = clock(), 0, 0
    stop = False
    while i < len(jobs) and not stop:
        if max_minutes and (clock() - started) / 60 >= max_minutes:
            notes.append(f"stopped after {max_minutes} min to stay inside free GitHub minutes")
            break
        size = min(parallel, per_run_cap - sent, gem.remaining(), len(jobs) - i)
        if size <= 0:
            if gem.remaining() <= 0:
                notes.append("daily Gemini budget used up")
            break
        wave, i, sent = jobs[i:i + size], i + size, sent + size
        with ThreadPoolExecutor(max_workers=size) as pool:
            results = list(pool.map(lambda j: _ask(gem, j, rules), wave))
        for job, (res, err) in zip(wave, results):
            c = job["candidate"]
            vid = str(c["video_id"])
            if err is not None:
                if isinstance(err, BudgetReached):
                    notes.append(str(err))
                    stop = True
                elif isinstance(err, Transient):
                    notes.append(f"{vid}: {err} (will retry next run)")
                else:
                    notes.append(f"{vid}: {err}")
                    watched_vid[vid] = "watch_failed"  # don't retry it every run
                continue
            answer, model = res
            rows = moments_from_answer(answer, job, used, block_ids, rules, now)
            out.extend(rows)
            for r in job["signal_rows"]:
                watched_sig[str(r["moment_id"])] = "watched"
            watched_vid[vid] = "watched"
            ready = sum(1 for r in rows if r["gate_status"] == "ready")
            notes.append(f"{vid} [{model}] {len(job['windows']) or 'whole'} windows -> "
                         f"{len(rows)} moments, {ready} ready | {c.get('title', '')[:50]}")
    return out, watched_sig, watched_vid, notes


def main():
    settings, rules = load_settings(), load_rules()
    wcfg = settings["gemini"]["watcher"]
    now = datetime.now(timezone.utc)
    try:
        client = sheets.get_client()
        ss = sheets.open_sheet(client, settings["sheet"]["id"])
        sheets.ensure_tab(ss, TAB, TABS[TAB])
        mom_ws, cand_ws = ss.worksheet(TAB), ss.worksheet("Candidates")
        moments = sheets.read_records(mom_ws)
        candidates = sheets.read_records(cand_ws)
        batch = sheets.read_records(ss.worksheet("Batch"))
        posted = sheets.read_records(ss.worksheet("Posted"))
    except sheets.SheetSetupError as e:
        print(f"WATCHER FAILED (sheet): {e}")
        return 1

    try:
        gem = Gemini(wcfg["models"], wcfg["daily_budget"], "watcher",
                     retry_wait=rules["watcher"].get("retry_wait_seconds", 20))
    except GeminiError as e:
        print(f"WATCHER FAILED: {e}")
        return 1
    per_run = math.ceil(wcfg["daily_budget"] / wcfg.get("runs_per_day", 1))
    jobs = pick_jobs(candidates, moments, rules)
    block_ids = {b if isinstance(b, str) else b.get("channel_id") for b in load_blocklist().get("channels", [])}
    print(f"{len(jobs)} videos waiting; Gemini calls left today: {gem.remaining()}/{gem.budget} "
          f"(this run max {per_run}).")
    rows, watched_sig, watched_vid, notes = run(
        gem, jobs, used_spans(batch, posted, moments), block_ids, rules, per_run, now,
        max_minutes=rules["watcher"].get("max_run_minutes"))
    for n in notes:
        print(f"  {n}")

    upd, new = sheets.upsert_rows(mom_ws, TABS[TAB], "moment_id", rows,
                                  preserve=("gate_status", "created_at"))
    if watched_sig:
        sheets.set_column(mom_ws, TABS[TAB], "moment_id", "gate_status", watched_sig)
    if watched_vid:
        sheets.set_column(cand_ws, TABS["Candidates"], "video_id", "status", watched_vid)
    ready = sum(1 for r in rows if r["gate_status"] == "ready")
    print(f"Moments: {new} new ({ready} ready), {upd} updated. Gemini calls this run: "
          f"{gem.calls_this_run}; used today: {gem.usage.used('watcher')}/{gem.budget}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
