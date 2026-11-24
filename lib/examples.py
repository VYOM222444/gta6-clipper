"""Examples tab: which editing tricks are going viral RIGHT NOW, with 10 real examples of each.

Tricks are ranked by how much more the Shorts winning right now use them than average Shorts
(last 72 h rising vs average when there is enough fresh data, else 7-day winners + rising vs
average). The top `tricks` get `per` example Shorts each (best breakouts first), plus a
"TOP breakouts" list of any style. The owner downloads them (Colab, agents/fetch_examples.py) and
sends them to Claude in chat to build the skill. Fixed slots, overwritten every Researcher run.
"""
from lib.skills import SKILLS

TOP = "TOP breakouts (any style)"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def describe(f):
    """Short human summary of a Short's editing from its Gemini tags."""
    bits = []
    if f.get("layout"):
        bits.append(str(f["layout"]).replace("_", " "))
    if f.get("caption_style") not in (None, "", "none"):
        bits.append(f"captions {str(f['caption_style']).replace('_', ' ')} {f.get('caption_position', '')}".strip())
    if f.get("hook_text_on_screen"):
        bits.append(f"hook \"{f['hook_text_on_screen']}\"")
    for key, name in (("zoom_ins", "zooms"), ("screen_shake", "shake"), ("freeze_frame", "freeze"),
                      ("slow_motion", "slow-mo"), ("emoji_or_stickers", "emoji"), ("loop_ending", "loop")):
        v = f.get(key)
        if v and v is not False:
            bits.append(name)
    sfx = [s for s in (f.get("sound_effects") or []) if s and s != "none"]
    if sfx:
        bits.append("sfx " + "/".join(sfx))
    if f.get("cuts"):
        bits.append(f"{f['cuts']} cuts")
    return ", ".join(bits)[:300]


def _share(feats, test):
    return sum(1 for f in feats if test(f)) / len(feats) if feats else 0.0


def viral_tricks(win, avg, min_use=0.15):
    """[{key, name, have, w, a, lift}] for every trick, most 'viral right now' first:
    used by >= min_use of winners and more than by average Shorts."""
    out = []
    for key, (name, test, have) in SKILLS.items():
        w, a = _share(win, test), _share(avg, test)
        out.append({"key": key, "name": name, "have": have, "w": w, "a": a, "lift": w - a})
    hot = [t for t in out if t["w"] >= min_use and t["lift"] > 0]
    return sorted(hot, key=lambda t: -t["lift"])


def build(entries, tricks_ranked, updated_at, tricks=5, per=10, top=10):
    """entries: [(row, features)] of winners / rising Shorts. -> Examples tab records."""
    ranked = sorted(entries, key=lambda e: (_num(e[0].get("breakout")), _num(e[0].get("views"))), reverse=True)

    def rec(slot, cat, trick_rank, rank, t, item):
        r, f = item if item else ({}, {})
        return {"example_id": f"{slot}#{rank}", "category": cat, "rank": rank,
                "have_skill": "" if item is None or t is None else ("yes" if t["have"] else "no"),
                "url": r.get("url", ""), "title": r.get("title", ""), "channel_title": r.get("channel_title", ""),
                "views": r.get("views", ""), "breakout": r.get("breakout", ""),
                "age_days": r.get("age_days", ""), "style": describe(f) if item else "",
                "updated_at": updated_at, "trick_rank": trick_rank,
                "trick_use": "" if t is None else f"winners {round(t['w'] * 100)}% vs average {round(t['a'] * 100)}%"}

    out = []
    for k in range(tricks):
        t = tricks_ranked[k] if k < len(tricks_ranked) else None
        hits = [e for e in ranked if t and SKILLS[t["key"]][1](e[1])][:per]
        for i in range(per):
            out.append(rec(f"trick{k + 1}", t["name"] if t else "", k + 1, i + 1, t,
                           hits[i] if i < len(hits) else None))
    for i in range(top):
        out.append(rec("top", TOP, "", i + 1, None, ranked[i] if i < len(ranked) else None))
    return out
