"""Signals: find WHERE the moments are inside candidate videos, with zero Gemini calls.

For the top candidates (finished live streams and long videos, best score first):
  - chat replay spikes   (yt-dlp, live streams only)   "LMAO / 💀 / CLIP IT" per 10 s
  - Most Replayed peaks  (yt-dlp video info "heatmap")
  - timestamped comments (YouTube API, 1 unit)          "4:32 💀"
Nearby windows from different signals are merged. Output -> `Moments` (source=signal,
gate_status=pending); the candidate's status becomes signals_done / no_signals.

Modes (env SIGNALS_MODE):
  api  - GitHub Actions (YouTube blocks yt-dlp there): comments only; candidate status
         -> comments_done. Timestamp comments pile up over hours, so videos younger than
         `signals.recheck_max_age_hours` are checked again every `recheck_every_hours`
         (time of the last check: Candidates.signals_at). New windows that overlap a
         moment we already have are skipped, so the Watcher only sees new spots.
  full - Colab notebook (colab/signals.ipynb): yt-dlp chat + Most Replayed, plus comments
         if YOUTUBE_API_KEY is set. Status -> signals_done / no_signals.
Set YTDLP_JS_RUNTIME (e.g. "node" in Colab) to pass --js-runtimes to yt-dlp.

Run:  SIGNALS_MODE=api python -m agents.signals
"""
import glob
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from agents.scout import title_ok
from lib import sheets
from lib.config import load_rules, load_settings
from lib.schema import TABS
from lib.youtube import QuotaExceeded, YouTube, YouTubeError

TAB = "Moments"
DONE_STATUS = {"signals_done", "no_signals", "blocked_leak", "blocked"}
API_DONE_STATUS = DONE_STATUS | {"comments_done", "watched", "watch_failed"}
RECHECK_STATUS = {"comments_done", "watched"}
BLOCK_HINTS = ("sign in to confirm", "not a bot", "http error 429", "too many requests",
               "unable to download api page", "precondition check failed")
TS_RE = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])")


# --- chat replay ---------------------------------------------------------------------

def parse_chat_line(line):
    """One yt-dlp live_chat JSON line -> [(offset_seconds, text)]."""
    try:
        d = json.loads(line)
    except ValueError:
        return []
    act = d.get("replayChatItemAction", {})
    try:
        offset = int(act.get("videoOffsetTimeMsec", 0)) / 1000
    except (TypeError, ValueError):
        return []
    out = []
    for a in act.get("actions", []):
        item = a.get("addChatItemAction", {}).get("item", {})
        r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        if not r:
            continue
        parts = []
        for run in r.get("message", {}).get("runs", []):
            if "text" in run:
                parts.append(run["text"])
            elif "emoji" in run:
                e = run["emoji"]
                parts.append((e.get("shortcuts") or [""])[0] if e.get("isCustomEmoji") else e.get("emojiId", ""))
        out.append((offset, " ".join(parts)))
    return out


def read_chat_file(path):
    msgs = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            msgs.extend(parse_chat_line(line))
    return msgs


def _reaction_matcher(words):
    alpha = [w for w in words if re.fullmatch(r"[a-z ]+", w)]
    other = [w for w in words if w not in alpha]
    # last letter may repeat: "lmao" also matches "LMAOOOO", "w" matches "www"
    pat = re.compile(r"\b(" + "|".join(re.escape(w) + "+" for w in alpha) + r")\b") if alpha else None

    def is_reaction(text):
        t = text.lower()
        return bool((pat and pat.search(t)) or any(o in t for o in other))
    return is_reaction


def chat_spikes(messages, cfg):
    """Windows where reaction messages jump far above the stream's normal level."""
    win = cfg["chat_window_seconds"]
    is_reaction = _reaction_matcher(cfg["reaction_words"])
    counts = {}
    for t, text in messages:
        if t >= 0 and is_reaction(text):
            b = int(t // win)
            counts[b] = counts.get(b, 0) + 1
    if not counts:
        return []
    n_bins = max(counts) + 1
    series = [counts.get(i, 0) for i in range(n_bins)]
    baseline = max(statistics.median(series), 1.0)
    out = []
    for i, c in enumerate(series):
        left = series[i - 1] if i else 0
        right = series[i + 1] if i + 1 < n_bins else 0
        if c >= cfg["chat_min_reactions"] and c >= cfg["chat_spike_vs_baseline"] * baseline \
                and c >= left and c > right:
            start = max(i * win - cfg["chat_lead_seconds"], 0)
            end = (i + 1) * win + cfg.get("chat_tail_seconds", 10)
            out.append({"start": start, "end": end, "signal": "chat",
                        "strength": c / baseline,
                        "note": f"chat {c / baseline:.1f}x normal ({c} reactions)"})
    return out


# --- Most Replayed --------------------------------------------------------------------

def replay_peaks(heatmap, duration, cfg):
    if not heatmap:
        return []
    pad, skip_until = cfg["replay_pad_seconds"], duration * cfg["replay_skip_start_share"]
    out = []
    for i, seg in enumerate(heatmap):
        v = seg.get("value", 0)
        left = heatmap[i - 1]["value"] if i else 0
        right = heatmap[i + 1]["value"] if i + 1 < len(heatmap) else 0
        if v >= cfg["replay_min_value"] and v >= left and v > right and seg["start_time"] >= skip_until:
            out.append({"start": max(seg["start_time"] - pad, 0), "end": seg["end_time"] + pad,
                        "signal": "replay", "strength": v,
                        "note": f"most replayed {v:.2f}"})
    return out


# --- timestamped comments ---------------------------------------------------------------

def comment_timestamps(text, duration):
    out = []
    for h, m, s in TS_RE.findall(text):
        secs = int(h or 0) * 3600 + int(m) * 60 + int(s)
        if int(s) < 60 and 0 < secs < duration:
            out.append(secs)
    return out


def comment_peaks(comments, duration, cfg):
    win = cfg["chat_window_seconds"]
    buckets = {}
    for text, likes in comments:
        for secs in set(comment_timestamps(text, duration)):
            b = secs // win
            n, w, first = buckets.get(b, (0, 0, secs))
            buckets[b] = (n + 1, w + 1 + likes, min(first, secs))
    out = []
    for b, (n, w, first) in buckets.items():
        if n >= cfg["comments_min_mentions"]:
            out.append({"start": max(first - cfg["comments_before_seconds"], 0),
                        "end": first + cfg["comments_after_seconds"], "signal": "comments",
                        "strength": float(n), "note": f"{n} comments ({w - n} likes)"})
    return out


# --- merging ----------------------------------------------------------------------------

def _points(w):
    s = w["strength"]
    return {"chat": min(s / 3, 3.0), "replay": s * 2, "comments": min(s / 2, 3.0)}[w["signal"]]


def merge_windows(windows, cfg):
    gap, max_len = cfg["merge_gap_seconds"], cfg["max_window_seconds"]
    merged = []
    for w in sorted(windows, key=lambda w: w["start"]):
        if merged and w["start"] <= merged[-1]["end"] + gap \
                and max(w["end"], merged[-1]["end"]) - merged[-1]["start"] <= max_len:
            m = merged[-1]
            m["end"] = max(m["end"], w["end"])
            m["signals"].add(w["signal"])
            m["notes"].append(w["note"])
            m["points"] += _points(w)
        else:
            merged.append({"start": w["start"], "end": w["end"], "signals": {w["signal"]},
                           "notes": [w["note"]], "points": _points(w)})
    for m in merged:
        if len(m["signals"]) > 1:
            m["points"] *= 1.5  # two independent signals agree
    merged.sort(key=lambda m: m["points"], reverse=True)
    return merged[: cfg["max_windows_per_video"]]


def to_moments(video_id, merged, now):
    rows = []
    for m in merged:
        start, end = int(m["start"]), int(m["end"])
        rows.append({
            "moment_id": f"{video_id}-{start}", "video_id": video_id,
            "start_s": start, "end_s": end, "source": "signal",
            "signal": "+".join(sorted(m["signals"])), "strength": round(m["points"], 2),
            "reason": "; ".join(m["notes"]), "gate_status": "pending",
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return rows


# --- choosing videos ----------------------------------------------------------------------

def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _time(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours_since(s, now):
    t = _time(s)
    return (now - t).total_seconds() / 3600 if t else None


def due_for_recheck(c, cfg, now):
    """api mode: a video already checked gets another comments pass while it is young."""
    if c.get("status") not in RECHECK_STATUS:
        return False
    age = _hours_since(c.get("published_at"), now)
    if age is None or age > cfg.get("recheck_max_age_hours", 0):
        return False
    since = _hours_since(c.get("signals_at"), now)
    return since is None or since >= cfg.get("recheck_every_hours", 6) - 0.5


def pick_videos(candidates, tracker_rows, rules, mode="full", now=None):
    sig, sc = rules["signals"], rules["scout"]
    now = now or datetime.now(timezone.utc)
    labels = {r["video_id"]: r.get("label", "") for r in tracker_rows}
    limit = sig.get("api_videos_per_run", sig["videos_per_run"]) if mode == "api" else sig["videos_per_run"]
    done = API_DONE_STATUS if mode == "api" else DONE_STATUS
    seen, fresh, again = set(), [], []
    for c in sorted(candidates, key=lambda c: _f(c.get("score")), reverse=True):
        vid = str(c.get("video_id", ""))
        if not vid or vid in seen:
            continue
        seen.add(vid)
        if c.get("kind") not in sig["kinds"]:
            continue
        if _f(c.get("duration_s")) < sig["min_duration_s"] and c.get("kind") != "live_vod":
            continue
        if labels.get(vid) in sig["skip_labels"] or not title_ok(c.get("title", ""), sc):
            continue
        if c.get("status") not in done:
            fresh.append(c)
        elif mode == "api" and due_for_recheck(c, sig, now):
            again.append(c)
    return (fresh + again)[:limit]


def drop_known(video_id, merged, moments, pad=10):
    """Skip windows that overlap a moment we already have for this video (any source)."""
    spans = [(_f(m.get("start_s")), _f(m.get("end_s"))) for m in moments
             if str(m.get("video_id")) == video_id]
    return [w for w in merged
            if not any(s - pad <= w["start"] <= e and w["end"] >= s - pad for s, e in spans)]


# --- yt-dlp ---------------------------------------------------------------------------------

def fetch_ytdlp(video_id, is_live_vod, workdir, timeout_s):
    """Returns (info_dict_or_None, chat_messages, error_text)."""
    out_tmpl = os.path.join(workdir, "%(id)s.%(ext)s")
    cmd = [sys.executable, "-m", "yt_dlp", "--skip-download", "--write-info-json",
           "--no-warnings", "--no-progress", "-o", out_tmpl]
    if os.environ.get("YTDLP_JS_RUNTIME"):
        cmd += ["--js-runtimes", os.environ["YTDLP_JS_RUNTIME"]]
    if is_live_vod:
        cmd += ["--write-subs", "--sub-langs", "live_chat"]
    cmd.append(f"https://www.youtube.com/watch?v={video_id}")
    err = ""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        if p.returncode != 0:
            err = (p.stderr or p.stdout).strip().splitlines()[-1:] or ["yt-dlp failed"]
            err = err[0]
    except subprocess.TimeoutExpired:
        err = f"chat download stopped after {timeout_s}s (using what was saved)"
    info = None
    for path in glob.glob(os.path.join(workdir, f"{glob.escape(video_id)}*.info.json")):
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
    msgs = []
    for path in glob.glob(os.path.join(workdir, f"{glob.escape(video_id)}*live_chat*")):
        msgs.extend(read_chat_file(path))
    return info, msgs, err


def is_blocked(err):
    e = err.lower()
    return any(h in e for h in BLOCK_HINTS)


# --- run --------------------------------------------------------------------------------------

def analyse(video, info, chat, comments, cfg):
    duration = _f((info or {}).get("duration")) or _f(video.get("duration_s")) or 10 ** 6
    windows = chat_spikes(chat, cfg)
    windows += replay_peaks((info or {}).get("heatmap") or [], duration, cfg)
    windows += comment_peaks(comments, duration, cfg)
    return merge_windows(windows, cfg)


def main():
    settings, rules = load_settings(), load_rules()
    cfg = {**rules["signals"]}
    mode = os.environ.get("SIGNALS_MODE", "full").lower()
    now = datetime.now(timezone.utc)
    try:
        client = sheets.get_client()
        ss = sheets.open_sheet(client, settings["sheet"]["id"])
        sheets.ensure_tab(ss, TAB, TABS[TAB])
        sheets.ensure_tab(ss, "Candidates", TABS["Candidates"])
        cand_ws, mom_ws = ss.worksheet("Candidates"), ss.worksheet(TAB)
        candidates = sheets.read_records(cand_ws)
        known = sheets.read_records(mom_ws)
        tracker_rows = sheets.read_records(ss.worksheet("Tracker"))
    except sheets.SheetSetupError as e:
        print(f"SIGNALS FAILED (sheet): {e}")
        return 1

    todo = pick_videos(candidates, tracker_rows, rules, mode, now)
    try:
        yt = YouTube(budget_units=settings["youtube"]["signals_run_budget_units"])
    except YouTubeError:
        if mode == "api":
            print("SIGNALS FAILED: YOUTUBE_API_KEY missing")
            return 1
        yt = None  # Colab without a YouTube key: chat + replay only
    print(f"Mode {mode}: checking {len(todo)} videos"
          + ("" if yt else " (no YouTube key: skipping comments)") + ".")
    all_moments, status, blocked = [], {}, 0
    with tempfile.TemporaryDirectory() as tmp:
        for c in todo:
            vid, live = str(c["video_id"]), c.get("kind") == "live_vod"
            info, chat, err = (None, [], "")
            if mode != "api":
                info, chat, err = fetch_ytdlp(vid, live, tmp, cfg["chat_timeout_seconds"])
                if err and is_blocked(err):
                    blocked += 1
            comments = []
            if yt:
                try:
                    comments = yt.top_comments(vid, cfg["comments_max"])
                except QuotaExceeded as e:
                    print(f"note: {e}")
                except YouTubeError as e:
                    print(f"note: comments for {vid} failed: {e}")
            merged = drop_known(vid, analyse(c, info, chat, comments, cfg), known)
            rows = to_moments(vid, merged, now)
            all_moments.extend(rows)
            upd = {"signals_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
            if mode == "api":
                if c.get("status") not in RECHECK_STATUS:
                    upd["status"] = "comments_done"
            elif info is not None:
                upd["status"] = "signals_done" if rows else "no_signals"
            status[vid] = upd
            heat = len((info or {}).get("heatmap") or [])
            again = " (re-check)" if c.get("status") in RECHECK_STATUS else ""
            print(f"  {vid}{again} | {c.get('kind'):<8} | chat msgs {len(chat):>6} | heatmap {heat:>3} | "
                  f"comments {len(comments):>3} | windows {len(rows)} | {c.get('title', '')[:45]}"
                  + (f" | yt-dlp: {err[:80]}" if err else ""))

    upd, new = sheets.upsert_rows(mom_ws, TABS[TAB], "moment_id", all_moments,
                                  preserve=("gate_status", "created_at"))
    if status:
        sheets.set_values(cand_ws, TABS["Candidates"], "video_id", status)
    print(f"Moments: {new} new, {upd} updated."
          + (f" Quota used: {yt.used}/{yt.budget} units." if yt else ""))
    if mode != "api" and todo and blocked == len(todo):
        print("YTDLP_BLOCKED: YouTube refused every yt-dlp request from this machine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
