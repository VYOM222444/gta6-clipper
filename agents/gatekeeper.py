"""Gatekeeper: last check on `ready` moments before the Decider picks the batch.

Runs after every Watcher run (chain) and can be re-run any time. For each Moments row with
source=watcher and gate_status=ready it may set:
  rejected_leak        source title / what happens mentions leaked or pre-release footage
  rejected_blocklist   creator is on config/blocklist.json (channel id or name)
  rejected_used        within `overlap_seconds` of a moment already in Batch / Posted
  rejected_duplicate   overlaps another ready moment of the same video with higher quality
  rejected_saturated   other Shorts already show this moment (saturation check below)
Rows that pass stay `ready` (gate_note says why others failed; `saturation` shows what
was found).

Saturation check (YouTube API, 100 units per source video): one search for Shorts that
mention the source creator, published after the source video. A found Short "matches" a
moment when it references the source (video id / creator name in title or description)
and shares >= `min_keyword_overlap` keywords with what happens in the moment. If a match
has >= `min_views` views, other clippers already own it -> rejected_saturated. Results are
cached per video in Candidates (sat_checked_at, sat_shorts) and refreshed every
`recheck_hours`; at most `checks_per_run` new searches per run.

Run:  python -m agents.gatekeeper   (needs YOUTUBE_API_KEY, GOOGLE_SERVICE_ACCOUNT_JSON)
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone

from agents.watcher import used_spans
from lib import sheets
from lib.config import load_blocklist, load_rules, load_settings
from lib.keywords import BASE_WORDS, STOPWORDS, tokens
from lib.schema import TABS
from lib.youtube import QuotaExceeded, YouTube, YouTubeError

TAB = "Moments"
EXTRA_STOP = {"clip", "moment", "video", "streamer", "player", "game", "gameplay", "shows",
              "while", "after", "before", "during", "gets", "goes", "makes", "being"}


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


def keywords_of(text):
    return {w for w in tokens(text) if len(w) >= 3 and not w.isdigit()
            and w not in STOPWORDS and w not in BASE_WORDS and w not in EXTRA_STOP}


# --- simple gates ------------------------------------------------------------------------------

def mentions_leak(text, words):
    t = (text or "").lower()
    return any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", t) for w in words)


def blocked(cand, blocklist):
    ids, names = set(), set()
    for b in blocklist.get("channels", []):
        if isinstance(b, str):
            ids.add(b)
        else:
            if b.get("channel_id"):
                ids.add(b["channel_id"])
            if b.get("name"):
                names.add(b["name"].strip().lower())
    return (cand.get("channel_id") in ids) or (str(cand.get("channel_title", "")).strip().lower() in names)


def simple_gate(m, cand, used, blocklist, cfg):
    """(status, note) for one ready moment, before duplicates / saturation."""
    if mentions_leak(f"{m.get('title', '')} {m.get('reason', '')}", cfg["leak_words"]):
        return "rejected_leak", "mentions leaked / pre-release footage"
    if blocked(cand, blocklist):
        return "rejected_blocklist", f"{cand.get('channel_title', '')} is on the blocklist"
    vid, start = str(m.get("video_id")), _f(m.get("start_s"))
    if any(v == vid and abs(s - start) <= cfg["overlap_seconds"] for v, s in used):
        return "rejected_used", "already in Batch / Posted"
    return "ready", ""


def overlap(a, b):
    s, e = max(_f(a["start_s"]), _f(b["start_s"])), min(_f(a["end_s"]), _f(b["end_s"]))
    shorter = min(_f(a["end_s"]) - _f(a["start_s"]), _f(b["end_s"]) - _f(b["start_s"])) or 1
    return max(e - s, 0) / shorter


def duplicates(ready, min_overlap):
    """moment_ids to drop: of two overlapping ready moments of one video, the weaker one."""
    drop = {}
    by_video = {}
    for m in ready:
        by_video.setdefault(str(m["video_id"]), []).append(m)
    for rows in by_video.values():
        rows.sort(key=lambda m: (-_f(m.get("quality")), _f(m.get("start_s"))))
        kept = []
        for m in rows:
            twin = next((k for k in kept if overlap(m, k) >= min_overlap), None)
            if twin:
                drop[str(m["moment_id"])] = f"same moment as {twin['moment_id']} (higher quality)"
            else:
                kept.append(m)
    return drop


# --- saturation ---------------------------------------------------------------------------------

def references_source(short, cand):
    text = f"{short.get('title', '')} {short.get('description', '')}".lower()
    name = str(cand.get("channel_title", "")).strip().lower()
    return str(cand.get("video_id", "")).lower() in text or (len(name) >= 4 and name in text)


def search_shorts(yt, cand, cfg):
    """Other Shorts about this source video -> compact list for the Candidates cache."""
    published = parse_time(cand.get("published_at")) or (datetime.now(timezone.utc) - timedelta(days=2))
    q = f"{cand.get('channel_title', '')} GTA 6".strip()
    found = yt.search_snippets(q, iso(published), duration="short", max_results=cfg["max_results"])
    found = [s for s in found if s["id"] != cand.get("video_id") and references_source(s, cand)]
    views = {}
    if found:
        for v in yt.videos([s["id"] for s in found], parts="statistics"):
            views[v["id"]] = int(v.get("statistics", {}).get("viewCount", 0) or 0)
    out = [{"id": s["id"], "v": views.get(s["id"], 0),
            "t": f"{s['title']} {s.get('description', '')}"[:250]} for s in found]
    out.sort(key=lambda s: -s["v"])
    return out[:15]


def saturation(m, shorts, cfg):
    """(is_saturated, note) for one moment against the Shorts found for its video."""
    kw = keywords_of(f"{m.get('reason', '')} {m.get('hook_idea', '')} {m.get('style_tags', '')}")
    matches = [s for s in shorts if len(kw & keywords_of(s["t"])) >= cfg["min_keyword_overlap"]]
    if not matches:
        return False, f"0 of {len(shorts)} Shorts about this video show it"
    top = max(matches, key=lambda s: s["v"])
    note = f"{len(matches)} Shorts already show it (top {top['v']:,} views: youtu.be/{top['id']})"
    return top["v"] >= cfg["min_views"], note


def needs_check(cand, now, cfg):
    t = parse_time(cand.get("sat_checked_at"))
    return t is None or now - t >= timedelta(hours=cfg["recheck_hours"])


# --- run ------------------------------------------------------------------------------------------

def run(moments, candidates, batch, posted, blocklist, rules, yt, now):
    """Returns (moment_updates, candidate_updates, notes)."""
    cfg = rules["gatekeeper"]
    sat_cfg = cfg["saturation"]
    cand = {str(c.get("video_id")): c for c in candidates}
    used = used_spans(batch, posted, moments)
    m_upd, c_upd, notes = {}, {}, []

    ready = []
    for m in moments:
        if m.get("source") != "watcher" or m.get("gate_status") != "ready":
            continue
        status, note = simple_gate(m, cand.get(str(m["video_id"]), {}), used, blocklist, cfg)
        if status != "ready":
            m_upd[str(m["moment_id"])] = {"gate_status": status, "gate_note": note}
        else:
            ready.append(m)
    for mid, note in duplicates(ready, cfg["duplicate_overlap"]).items():
        m_upd[mid] = {"gate_status": "rejected_duplicate", "gate_note": note}
    ready = [m for m in ready if str(m["moment_id"]) not in m_upd]

    # saturation: refresh the cache for the videos with the best ready moments first
    best = {}
    for m in ready:
        vid = str(m["video_id"])
        best[vid] = max(best.get(vid, 0), _f(m.get("quality")))
    checks = 0
    for vid in sorted(best, key=lambda v: -best[v]):
        c = cand.get(vid)
        if not c or not needs_check(c, now, sat_cfg) or yt is None:
            continue
        if checks >= sat_cfg["checks_per_run"]:
            notes.append(f"saturation: {len(best) - checks} more videos wait for the next run")
            break
        try:
            shorts = search_shorts(yt, c, sat_cfg)
        except QuotaExceeded as e:
            notes.append(f"saturation stopped: {e}")
            break
        except YouTubeError as e:
            notes.append(f"saturation check for {vid} failed: {e}")
            continue
        checks += 1
        c["sat_shorts"] = json.dumps(shorts, ensure_ascii=False)
        c["sat_checked_at"] = iso(now)
        c_upd[vid] = {"sat_shorts": c["sat_shorts"], "sat_checked_at": c["sat_checked_at"]}

    for m in ready:
        c = cand.get(str(m["video_id"]), {})
        try:
            shorts = json.loads(c.get("sat_shorts") or "null")
        except ValueError:
            shorts = None
        if shorts is None:
            continue  # not checked yet: stays ready
        sat, note = saturation(m, shorts, sat_cfg)
        upd = {"saturation": note}
        if sat:
            upd.update(gate_status="rejected_saturated", gate_note="other clippers already posted it")
        m_upd[str(m["moment_id"])] = upd
    return m_upd, c_upd, notes


def main():
    settings, rules = load_settings(), load_rules()
    now = datetime.now(timezone.utc)
    try:
        ss = sheets.open_sheet(sheets.get_client(), settings["sheet"]["id"])
        for tab in (TAB, "Candidates"):
            sheets.ensure_tab(ss, tab, TABS[tab])
        m_ws, c_ws = ss.worksheet(TAB), ss.worksheet("Candidates")
        moments, candidates = sheets.read_records(m_ws), sheets.read_records(c_ws)
        batch = sheets.read_records(ss.worksheet("Batch"))
        posted = sheets.read_records(ss.worksheet("Posted"))
    except sheets.SheetSetupError as e:
        print(f"GATEKEEPER FAILED (sheet): {e}")
        return 1
    try:
        yt = YouTube(budget_units=settings["youtube"]["gatekeeper_run_budget_units"])
    except YouTubeError as e:
        print(f"note: no saturation check ({e})")
        yt = None

    m_upd, c_upd, notes = run(moments, candidates, batch, posted, load_blocklist(), rules, yt, now)
    for n in notes:
        print(f"note: {n}")
    sheets.set_values(m_ws, TABS[TAB], "moment_id", m_upd)
    sheets.set_values(c_ws, TABS["Candidates"], "video_id", c_upd)
    counts = {}
    for u in m_upd.values():
        s = u.get("gate_status", "ready (checked)")
        counts[s] = counts.get(s, 0) + 1
    still = sum(1 for m in moments if m.get("source") == "watcher" and m.get("gate_status") == "ready"
                and m_upd.get(str(m["moment_id"]), {}).get("gate_status", "ready") == "ready")
    print(f"Gatekeeper: {still} moments ready for the batch; changes: {counts or 'none'}; "
          f"saturation searches: {len(c_upd)}" + (f"; quota used {yt.used}/{yt.budget} units." if yt else "."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
