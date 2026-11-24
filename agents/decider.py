"""Decider (automatic): every evening, pick today's clips, give each ONE brand, write the text.

1. pick     (plain Python) ready moments (Gatekeeper passed) from the last
            `max_moment_age_hours`, scored: Gemini quality + Tracker label of the source
            video (Breakout / Rising beat Fading) - age - "already partly clipped" notes.
            At most `max_per_video` per source video and `max_per_creator` per creator.
2. assign   (plain Python) one brand per clip, `clips_per_brand` each (ramp by date in
            settings), spreading content types and creators across brands.
3. write    (one Gemini text call, Flash -> Flash-Lite) hook_text, title_label, title,
            description line 1, 1-2 topic hashtags, Instagram first line, using today's
            rising Keywords and the Researcher's SEO findings. If Gemini fails, simple text
            is built from the Watcher's hook idea, so a batch is always made.
4. publish  every clip is fixed to the Playbook limits, validated with publish_batch
            and written to the Batch tab (auto-approved). batches/<date>.json is saved too.

Safe to re-run: today's clips already in Batch count toward the targets (top-up only).
Run:  python -m agents.decider   (needs GEMINI_API_KEY, GOOGLE_SERVICE_ACCOUNT_JSON)
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from agents import publish_batch
from lib import style as clip_style
from lib import sheets
from lib.config import REPO_ROOT, load_rules, load_settings
from lib.gemini import Gemini, GeminiError, Usage
from lib.schema import TABS

IST = ZoneInfo("Asia/Kolkata")
USAGE_FILE = REPO_ROOT / "data" / "gemini_usage_decider.json"
TAG_RE = re.compile(r"[^0-9A-Za-z]")

SCHEMA = {
    "type": "OBJECT",
    "properties": {"clips": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {
            "n": {"type": "INTEGER"},
            "hook_text": {"type": "STRING"},
            "title_label": {"type": "STRING"},
            "title": {"type": "STRING"},
            "what_happens": {"type": "STRING"},
            "topic_hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
            "ig_first_line": {"type": "STRING"},
        },
        "required": ["n", "hook_text", "title_label", "title", "what_happens", "topic_hashtags",
                     "ig_first_line"],
    }}},
    "required": ["clips"],
}


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def parse_time(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def today_ist(now):
    return now.astimezone(IST).date().isoformat()


def clips_per_brand(settings, day):
    """Ramp by date (latest date <= today wins), else batch.clips_per_brand."""
    b = settings["batch"]
    ramp = b.get("clips_per_brand_by_date") or {}
    past = [d for d in ramp if d <= day]
    return int(ramp[max(past)]) if past else int(b["clips_per_brand"])


# --- 1. pick --------------------------------------------------------------------------------

def moment_score(m, label, now, cfg):
    s = _f(m.get("quality"))
    s += cfg["label_bonus"].get(label or "", 0)
    t = parse_time(m.get("watched_at") or m.get("created_at"))
    if t:
        s -= cfg["age_penalty_per_hour"] * max((now - t).total_seconds() / 3600 - 6, 0)
    found = re.match(r"(\d+) Shorts already", str(m.get("saturation", "")))
    if found:
        s -= cfg["partly_clipped_penalty"] * int(found.group(1))
    return round(s, 3)


def pick(moments, tracker, cand, now, cfg, n):
    labels = {str(t.get("video_id")): t.get("label", "") for t in tracker}
    fresh = []
    for m in moments:
        if m.get("source") != "watcher" or m.get("gate_status") != "ready":
            continue
        t = parse_time(m.get("watched_at") or m.get("created_at"))
        if t and now - t > timedelta(hours=cfg["max_moment_age_hours"]):
            continue
        fresh.append((moment_score(m, labels.get(str(m["video_id"])), now, cfg), m))
    fresh.sort(key=lambda x: -x[0])
    per_video, per_creator, out = {}, {}, []
    for score, m in fresh:
        vid = str(m["video_id"])
        creator = str(cand.get(vid, {}).get("channel_id") or m.get("channel_title", ""))
        if per_video.get(vid, 0) >= cfg["max_per_video"] or per_creator.get(creator, 0) >= cfg["max_per_creator"]:
            continue
        per_video[vid] = per_video.get(vid, 0) + 1
        per_creator[creator] = per_creator.get(creator, 0) + 1
        out.append({**m, "_score": score, "_creator": creator})
        if len(out) >= n:
            break
    return out


# --- 2. assign ------------------------------------------------------------------------------

def assign(picked, slots, existing=None):
    """slots: {brand: free slots}. existing: {brand: [content types / creators already today]}.
    Best clip first; each goes to the brand with the most free slots, preferring brands that
    don't have this content type / creator yet."""
    have = {b: list((existing or {}).get(b, [])) for b in slots}
    free = dict(slots)
    out = []
    for m in picked:
        options = [b for b in free if free[b] > 0]
        if not options:
            break
        ct, cr = m.get("content_type", ""), m["_creator"]
        best = max(options, key=lambda b: (free[b], -have[b].count(ct), -have[b].count(cr)))
        free[best] -= 1
        have[best] += [ct, cr]
        out.append({**m, "_brand": best})
    return out


# --- 3. write -------------------------------------------------------------------------------

def build_prompt(clips, keywords, seo, rules, hook_style=""):
    pk, hk, tl = rules["packaging"], rules["hook"], rules["title_label"]
    lines = []
    for i, m in enumerate(clips, start=1):
        lines.append(f"{i}. [{m.get('content_type', '')}] {m.get('reason', '')} | hook idea: "
                     f"{m.get('hook_idea', '')} | source: \"{m.get('title', '')}\" by {m.get('channel_title', '')}")
    kw = ", ".join(keywords) or "(none today)"
    return f"""You write packaging for GTA 6 YouTube Shorts / Instagram Reels made from other creators' clips.
For EACH clip below return:
- hook_text: on-screen hook shown in the first 2 seconds, max {hk['max_words']} words, works with sound off, no hashtags.
  It must make someone stop scrolling: specific to THIS clip (what is about to happen or the
  surprise), never generic ("Watch this", "GTA 6 moment", "Wait for it" alone are not allowed).{hook_style}
- title_label: {tl['min_words']}-{tl['max_words']} words, CAPS, says what the clip is (shown on top the whole time).
- title: YouTube title, max {pk['title_max_chars']} characters, START with the search phrase people type
  (e.g. "GTA 6 police chase ..."), must contain "GTA 6", may end with one emoji.
- what_happens: one short plain sentence about the clip that includes "GTA 6".
- topic_hashtags: 1-2 hashtags about the topic (e.g. #GTA6Police), no #GTA6/#Shorts (added automatically).
- ig_first_line: Instagram caption first line (max {pk.get('ig_first_line_max_chars', 125)} chars): keywords first + what happens, 1 emoji.
Use these trending search words where they honestly fit: {kw}.
SEO findings from winning GTA 6 Shorts: {seo or '(none yet)'}.
Never claim something the clip doesn't show. No clickbait lies. If the source title is Hindi, write the
title and hook in Hinglish (Latin letters) with English GTA keywords.
Return one entry per clip with `n` = its number.

Clips:
""" + "\n".join(lines)


def fallback_text(m):
    hook = " ".join(str(m.get("hook_idea") or m.get("reason") or "Watch this").split()[:6])
    return {"hook_text": hook, "title_label": " ".join(str(m.get("style_tags", "GTA 6 moment")).split(",")[0].split()[:4]) or "GTA 6 MOMENT",
            "title": f"GTA 6 {m.get('reason', '')}", "what_happens": f"GTA 6: {m.get('reason', '')}",
            "topic_hashtags": [], "ig_first_line": f"GTA 6 {m.get('reason', '')}"}


def _cut(text, n):
    text = " ".join(str(text).split())
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0].rstrip(",.;:-–")


def finish_clip(m, text, rules):
    """Turn Gemini's (or fallback) text into a valid batch clip."""
    pk, hk, tl = rules["packaging"], rules["hook"], rules["title_label"]
    credit = (m.get("channel_title") or "the creator").strip()
    hook = " ".join(str(text.get("hook_text", "")).replace("#", "").split()[:hk["max_words"]])
    label_words = str(text.get("title_label", "")).upper().split()[:tl["max_words"]]
    while len(label_words) < tl["min_words"]:
        label_words.append("GTA 6" if not label_words else "MOMENT")
    title = str(text.get("title", "")).strip()
    if "gta 6" not in title.lower() and "gta vi" not in title.lower():
        title = "GTA 6 " + title
    title = _cut(title, pk["title_max_chars"])
    topic = []
    for t in text.get("topic_hashtags") or []:
        t = "#" + TAG_RE.sub("", str(t))
        if len(t) > 2 and t.lower() not in {b.lower() for b in pk["base_hashtags"]} and t not in topic:
            topic.append(t)
    hashtags = " ".join(pk["base_hashtags"] + topic[:2])
    line1 = _cut(text.get("what_happens") or m.get("reason", ""), 150)
    description = f"{line1}\n🎥 Clip from {credit}\n{pk['disclaimer']}"
    ig1 = _cut(text.get("ig_first_line") or title, pk.get("ig_first_line_max_chars", 125))
    ig = f"{ig1}\n🎥 {credit}\n{pk['disclaimer']}\n\n{hashtags}"
    return {"moment_id": str(m["moment_id"]), "brand": m["_brand"], "content_type": m.get("content_type") or "chaos",
            "hook_text": hook, "title_label": " ".join(label_words), "title": title,
            "description": description, "hashtags": hashtags, "creator_credit": credit, "ig_caption": ig}


def write_text(gem, clips, keywords, seo, rules, hook_style=""):
    """{index: text dict}. Empty dict if Gemini can't help (fallback text is used)."""
    if not clips or gem is None:
        return {}, "no Gemini"
    try:
        answer, model = gem.generate_json([], build_prompt(clips, keywords, seo, rules, hook_style), SCHEMA, temperature=0.7)
    except GeminiError as e:
        return {}, f"Gemini failed ({e}); simple text used"
    return {int(c.get("n", 0)): c for c in answer.get("clips", [])}, model


# --- run ------------------------------------------------------------------------------------

def rising_keywords(rows, now, hours=12, top=12):
    out = []
    for r in sorted(rows, key=lambda r: -_f(r.get("score"))):
        t = parse_time(r.get("updated_at"))
        if t and now - t <= timedelta(hours=hours) and r.get("trend") in ("rising", "new", "steady"):
            out.append(str(r["phrase"]))
        if len(out) >= top:
            break
    return out


def seo_findings(rows, top=8):
    """Researcher SEO DO-rules: the "right now" (0-72 h) ones first, then the 7-day ones."""
    def pick(prefix, label):
        return [f"{label}{r.get('rule', '')}" for r in rows
                if str(r.get("section", "")).startswith(prefix) and str(r.get("rule", "")).startswith("DO")]
    lines = pick("research.now.seo", "now: ") + pick("research.seo", "")
    return "; ".join(lines[:top])


HOOK_KINDS = {"question": "a question the clip answers", "bold_claim": "a bold claim",
              "reaction": "a short reaction (like \"BRO WHAT\")", "warning": "a warning",
              "number_or_list": "a number or list", "pov": "a POV line (\"POV: ...\")"}


def hook_guidance(playbook_rows, settings):
    """Hook style from what winning Shorts do (Playbook style.* rows) -> extra prompt lines."""
    st, _, examples = clip_style.from_playbook(playbook_rows, settings["render"].get("style_defaults") or {})
    out = ""
    kind = HOOK_KINDS.get(str(st.get("hook_kind") or ""))
    if kind:
        out += f"\n  Winning GTA 6 Shorts mostly open with {kind}; about {st.get('hook_words', 5)} words."
    if examples:
        out += "\n  Real hooks from winning Shorts (copy the feel, not the words): " + \
               " | ".join(f'"{e}"' for e in examples[:6])
    return out


def plan(moments, tracker, candidates, batch_rows, settings, rules, now):
    """Returns (batch_dict, picked_with_brand) without any Gemini text."""
    cfg = settings["decider"]
    day = today_ist(now)
    per_brand = clips_per_brand(settings, day)
    brands = [b["key"] for b in settings["brands"]]
    todays = [b for b in batch_rows if str(b.get("batch_id", "")).startswith(day + "-")]
    existing = {b: [] for b in brands}
    for r in todays:
        if r.get("brand") in existing:
            existing[r["brand"]] += [r.get("content_type", ""), ""]
    slots = {b: max(per_brand - sum(1 for r in todays if r.get("brand") == b), 0) for b in brands}
    want = sum(slots.values())
    cand = {str(c.get("video_id")): c for c in candidates}
    picked = assign(pick(moments, tracker, cand, now, cfg, want), slots, existing)
    batch = {"batch_id": day, "start_index": len(todays) + 1, "clips": []}
    return batch, picked


def main():
    settings, rules = load_settings(), load_rules()
    now = datetime.now(timezone.utc)
    try:
        ss = sheets.open_sheet(sheets.get_client(), settings["sheet"]["id"])
        for tab in ("Batch", "Moments", "Keywords", "Playbook"):
            sheets.ensure_tab(ss, tab, TABS[tab])
        b_ws, m_ws = ss.worksheet("Batch"), ss.worksheet("Moments")
        batch_rows, moments = sheets.read_records(b_ws), sheets.read_records(m_ws)
        tracker = sheets.read_records(ss.worksheet("Tracker"))
        candidates = sheets.read_records(ss.worksheet("Candidates"))
        keywords = rising_keywords(sheets.read_records(ss.worksheet("Keywords")), now)
        playbook = sheets.read_records(ss.worksheet("Playbook"))
        seo = seo_findings(playbook)
        hook_style = hook_guidance(playbook, settings)
    except sheets.SheetSetupError as e:
        print(f"DECIDER FAILED (sheet): {e}")
        return 1

    batch, picked = plan(moments, tracker, candidates, batch_rows, settings, rules, now)
    day = batch["batch_id"]
    print(f"{day}: {clips_per_brand(settings, day)} clips per brand; {len(picked)} new clips picked "
          f"(start at #{batch['start_index']}).")
    if not picked:
        print("Nothing to add (targets reached or no ready moments).")
        return 0

    gcfg = settings["gemini"]["decider"]
    try:
        gem = Gemini(gcfg["models"], gcfg["daily_budget"], "decider", usage=Usage(USAGE_FILE),
                     retry_wait=45, requests_per_minute=5)
    except GeminiError as e:
        print(f"note: {e}")
        gem = None
    texts, how = write_text(gem, picked, keywords, seo, rules, hook_style)
    print(f"Text: {how}. Keywords used: {', '.join(keywords) or 'none'}.")

    batch["clips"] = [finish_clip(m, texts.get(i) or fallback_text(m), rules) for i, m in enumerate(picked, 1)]
    moments_by_id = {str(m["moment_id"]): m for m in moments}
    good, bad = publish_batch.build_rows(batch, moments_by_id, batch_rows, settings, rules)
    for clip_id, problems in bad:
        print(f"  SKIPPED {clip_id}: " + "; ".join(problems))
    upd, new = sheets.upsert_rows(b_ws, TABS["Batch"], "batch_id", good,
                                  preserve=("approved", "fetch_status", "render_status"))
    if good:
        sheets.set_column(m_ws, TABS["Moments"], "moment_id", "gate_status",
                          {g["moment_id"]: "in_batch" for g in good})
    path = REPO_ROOT / "batches" / f"{day}.json"
    path.parent.mkdir(exist_ok=True)
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"batch_id": day, "clips": []}
    old["clips"] += [c for c in batch["clips"] if c["moment_id"] in {g["moment_id"] for g in good}]
    old.pop("start_index", None)
    path.write_text(json.dumps(old, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for g in good:
        print(f"  {g['batch_id']} {g['brand']:<16} {g['content_type']:<8} {g['title']}")
    print(f"Batch: {new} new clips written (auto-approved: {settings['batch'].get('auto_approve', False)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
