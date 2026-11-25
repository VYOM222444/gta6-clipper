"""Clip style chosen from what winning GTA 6 Shorts do (no fixed template).

The Researcher calls `choose(...)` with the blind Gemini tags of winners and average Shorts and
writes the result to the Playbook tab as `style.*` rows (value + the reason). The Renderer reads
them with `from_playbook(...)`; the Decider reads the hook style + winning hook examples.

Every element is ON only when winners actually use it:
  bool element  -> on if >= 50% of winners use it, or winners use it clearly more than average
                   Shorts (>= min_diff more) and at least 30% of winners do.
  captions      -> judged only on winners that have speech (music-only edits have nothing to caption).
  enums         -> the most common choice among winners.
Not enough data -> settings.render.style_defaults.
"""
import json
import statistics
from collections import Counter

KEYS = ("title_label", "hook_text", "hook_kind", "hook_words", "captions", "caption_position",
        "caption_keyword_color", "layout")
CAPTION_STYLES = {"word_by_word": "word_by_word", "phrase_by_phrase": "phrase", "static_text": "phrase"}
LAYOUTS = {"full_vertical": "fill", "blurred_background": "blur", "black_bars": "black"}
TEXT_HOOKS = ("text_question", "text_statement")


def _share(items, test):
    return sum(1 for x in items if test(x)) / len(items) if items else 0.0


def _pct(x):
    return f"{round(x * 100)}%"


def _bool_choice(win, avg, test, min_diff):
    w, a = _share(win, test), _share(avg, test)
    on = w >= 0.5 or (w - a >= min_diff and w >= 0.3)
    return on, f"{_pct(w)} of winners (average Shorts {_pct(a)})"


def _mode(values):
    values = [v for v in values if v]
    if not values:
        return None, 0.0
    v, n = Counter(values).most_common(1)[0]
    return v, n / len(values)


def has_speech(f):
    lang = str(f.get("spoken_language") or "").strip().lower()
    return bool(f.get("voiceover")) or (lang not in ("", "none", "no", "n/a"))


def choose(win, avg, defaults, min_winners=8, min_diff=0.15):
    """win/avg: lists of feature dicts. Returns (style, reasons, examples)."""
    style, why = dict(defaults), {k: "default (not enough analysed winners yet)" for k in KEYS}
    examples = []
    if len(win) < min_winners:
        return style, why, examples

    style["title_label"], why["title_label"] = _bool_choice(win, avg, lambda f: f.get("has_title_label") is True, min_diff)

    # hook text: the new exact tag when enough Shorts have it, else the hook_type proxy
    tagged_w = [f for f in win if "hook_text_kind" in f]
    if len(tagged_w) >= min_winners:
        tagged_a = [f for f in avg if "hook_text_kind" in f]
        style["hook_text"], why["hook_text"] = _bool_choice(
            tagged_w, tagged_a, lambda f: f.get("hook_text_kind") not in (None, "", "none"), min_diff)
        kind, share = _mode([f.get("hook_text_kind") for f in tagged_w if f.get("hook_text_kind") != "none"])
        if kind:
            style["hook_kind"], why["hook_kind"] = kind, f"most common among winners' hooks ({_pct(share)})"
        examples = [str(f.get("hook_text_on_screen")).strip() for f in tagged_w
                    if str(f.get("hook_text_on_screen") or "").strip()]
        lens = [len(e.split()) for e in examples]
        if lens:
            style["hook_words"] = max(2, min(8, int(statistics.median(lens))))
            why["hook_words"] = f"median length of {len(lens)} winning hooks"
    else:
        style["hook_text"], why["hook_text"] = _bool_choice(
            win, avg, lambda f: f.get("hook_type") in TEXT_HOOKS, min_diff)
        why["hook_text"] += " start with a big text hook (estimate from hook type)"

    speak_w, speak_a = [f for f in win if has_speech(f)], [f for f in avg if has_speech(f)]
    if len(speak_w) >= max(4, min_winners // 2):
        on, reason = _bool_choice(speak_w, speak_a, lambda f: f.get("caption_style") not in (None, "", "none"), min_diff)
        why["captions"] = reason.replace("of winners", "of winners with speech")
        if on:
            mode, share = _mode([CAPTION_STYLES.get(f.get("caption_style")) for f in speak_w])
            style["captions"] = mode or defaults.get("captions", "phrase")
            why["captions"] += f"; style {style['captions']} ({_pct(share)})"
            capped = [f for f in speak_w if f.get("caption_style") not in (None, "", "none")]
            pos, pshare = _mode([f.get("caption_position") for f in capped if f.get("caption_position") != "none"])
            if pos:
                style["caption_position"], why["caption_position"] = pos, f"{_pct(pshare)} of captioned winners"
            style["caption_keyword_color"], why["caption_keyword_color"] = _bool_choice(
                capped, [f for f in speak_a if f.get("caption_style") not in (None, "", "none")],
                lambda f: f.get("caption_keyword_color") is True, min_diff)
        else:
            style["captions"] = "none"

    lay, share = _mode([LAYOUTS.get(f.get("layout")) for f in win])
    if lay:
        style["layout"], why["layout"] = lay, f"{_pct(share)} of winners"
    return style, why, examples[:8]


def playbook_rows(style, why, examples, source, updated_at):
    rows = []
    for k in KEYS:
        v = style.get(k)
        shown = "on" if v is True else "off" if v is False else str(v)
        rows.append({"rule_id": f"style.{k}", "section": "style", "rule": f"{k}: {shown} ({why.get(k, '')})",
                     "value": json.dumps(v), "source": source, "updated_at": updated_at})
    rows.append({"rule_id": "style.hook_examples", "section": "style",
                 "rule": f"winning hook texts: {' | '.join(examples) or '(none tagged yet)'}",
                 "value": json.dumps(examples, ensure_ascii=False), "source": source, "updated_at": updated_at})
    return rows


def from_playbook(rows, defaults):
    """style.* Playbook rows -> (style dict, reasons, examples). Missing rows -> defaults."""
    style, why, examples = dict(defaults), {k: "default" for k in KEYS}, []
    for r in rows or []:
        rid = str(r.get("rule_id", ""))
        if not rid.startswith("style."):
            continue
        try:
            v = json.loads(r.get("value") or "null")
        except (ValueError, TypeError):
            continue
        k = rid[len("style."):]
        if k == "hook_examples":
            examples = [str(x) for x in v or []]
        elif k in KEYS and v is not None:
            style[k], why[k] = v, str(r.get("rule", ""))
    return style, why, examples


def tag(style):
    """Short tag for Batch.template_version, e.g. 'phrase-middle,hook,nolabel,blur'."""
    parts = [style.get("captions", "none") if style.get("captions") == "none"
             else f"{style.get('captions')}-{style.get('caption_position', 'bottom')}",
             "hook" if style.get("hook_text") else "nohook",
             "label" if style.get("title_label") else "nolabel",
             str(style.get("layout", "blur"))]
    return ",".join(parts)


# --- clip length (winners' typical length) -----------------------------------------------------

def clip_length(win_lengths, avg_lengths, min_n=8, lo=15, hi=60):
    """Longest clip we should make, from how long winning Shorts are.
    Returns (max_seconds or None, reason). max = MEDIAN length of winners (the 75th percentile was
    52 s on real data, above every content type's own limit, so it changed nothing); clamped."""
    w = sorted(float(x) for x in win_lengths if x)
    if len(w) < min_n:
        return None, f"not enough winners with a length ({len(w)} < {min_n})"
    q = statistics.quantiles(w, n=4)  # q[1] = median, q[2] = 75th percentile
    cap = int(round(min(hi, max(lo, q[1]))))
    a = [float(x) for x in avg_lengths if x]
    avg = f", average Shorts median {round(statistics.median(a))} s" if a else ""
    return cap, f"winners median {round(q[1])} s, 75% are <= {round(q[2])} s{avg} (n={len(w)})"


def length_row(cap, reason, source, updated_at):
    shown = f"{cap} s" if cap else "no cap (default lengths)"
    return {"rule_id": "style.clip_max_seconds", "section": "style",
            "rule": f"clip_max_seconds: {shown} ({reason})", "value": json.dumps(cap),
            "source": source, "updated_at": updated_at}


def apply_length(rules, playbook_rows, min_gap=5):
    """Copy of rules with every content type's clip length capped at the winners' length
    (Playbook style.clip_max_seconds). No row / no value -> rules unchanged."""
    cap = None
    for r in playbook_rows or []:
        if r.get("rule_id") == "style.clip_max_seconds":
            try:
                cap = json.loads(r.get("value") or "null")
            except (TypeError, ValueError):
                cap = None
    if not cap:
        return rules, None
    out = dict(rules)
    lengths = {}
    for t, (a, b) in (rules.get("clip_length_seconds") or {}).items():
        b2 = min(b, cap)
        lengths[t] = [min(a, max(8, b2 - min_gap)), b2]
    out["clip_length_seconds"] = lengths
    w = dict(rules.get("watcher", {}))
    if "clip_max_seconds" in w:
        w["clip_max_seconds"] = min(w["clip_max_seconds"], cap)
        w["clip_min_seconds"] = min(w.get("clip_min_seconds", 8), max(8, w["clip_max_seconds"] - min_gap))
    out["watcher"] = w
    return out, cap


# --- story structure (how winners start, go through the middle, and end) -----------------------

STRUCT_ENUMS = ("opening_type", "middle_type", "ending_type")
STRUCT_NUMS = ("payoff_pct", "setup_seconds", "beats", "dead_air_seconds")


def structure(win, avg, min_n=8):
    """Story shape of winning Shorts from their Gemini tags. -> (shape dict or None, reasons).
    Each part = the value winners use most MORE than average Shorts (>= 20% of winners), else the
    most common among winners; numbers = winners' medians."""
    w = [f for f in win if f.get("opening_type")]
    a = [f for f in avg if f.get("opening_type")]
    if len(w) < min_n:
        return None, f"not enough winners tagged with story structure yet ({len(w)} < {min_n})"
    shape, why = {}, {}
    for k in STRUCT_ENUMS:
        vals = sorted({f.get(k) for f in w if f.get(k)})
        if not vals:
            continue
        share = {v: (_share(w, lambda f: f.get(k) == v), _share(a, lambda f: f.get(k) == v)) for v in vals}
        lifted = [v for v in vals if share[v][0] >= 0.2 and share[v][0] > share[v][1]]
        best = max(lifted or vals, key=lambda v: (share[v][0] - share[v][1]) if lifted else share[v][0])
        shape[k] = best
        why[k] = f"{_pct(share[best][0])} of winners vs {_pct(share[best][1])} of average"
    for k in STRUCT_NUMS:
        wv = [float(f[k]) for f in w if isinstance(f.get(k), (int, float))]
        av = [float(f[k]) for f in a if isinstance(f.get(k), (int, float))]
        if wv:
            shape[k] = round(statistics.median(wv), 2)
            why[k] = f"winners {round(statistics.median(wv), 2)} vs average " + (
                str(round(statistics.median(av), 2)) if av else "?")
    shape["n"] = len(w)
    return shape, why


def describe_structure(shape):
    """One readable line, also used in the Watcher's prompt."""
    if not shape:
        return ""
    nice = lambda v: str(v).replace("_", " ")
    bits = []
    if shape.get("opening_type"):
        bits.append(f"START: {nice(shape['opening_type'])}"
                    + (f", action within {shape['setup_seconds']:g} s" if "setup_seconds" in shape else ""))
    if shape.get("middle_type"):
        bits.append(f"MIDDLE: {nice(shape['middle_type'])}"
                    + (f", about {shape['beats']:g} beats" if "beats" in shape else "")
                    + (f", no more than {shape['dead_air_seconds']:g} s of dead air" if "dead_air_seconds" in shape else ""))
    if shape.get("ending_type"):
        bits.append(f"END: {nice(shape['ending_type'])}"
                    + (f", payoff at ~{round(shape['payoff_pct'] * 100)}% of the clip" if "payoff_pct" in shape else ""))
    return "; ".join(bits)


def structure_row(shape, why, source, updated_at):
    if not shape:
        text = f"structure: not known yet ({why})"
    else:
        parts = [f"{k} {shape[k]} ({why.get(k, '')})" for k in STRUCT_ENUMS + STRUCT_NUMS if k in shape]
        text = f"structure: {describe_structure(shape)} | " + "; ".join(parts)
    return {"rule_id": "style.structure", "section": "style", "rule": text[:900],
            "value": json.dumps(shape), "source": source, "updated_at": updated_at}


def structure_from_playbook(rows):
    for r in rows or []:
        if r.get("rule_id") == "style.structure":
            try:
                return json.loads(r.get("value") or "null")
            except (TypeError, ValueError):
                return None
    return None
