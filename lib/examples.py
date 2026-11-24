"""Examples tab: real winning GTA 6 Shorts to copy a style from, one list per editing trick.

The owner opens the tab, picks a few, screen-records them and sends them to Claude in chat; Claude
studies the frames and builds the skill. Written by the Researcher's findings step every run.
Slots are fixed (`<category>#<rank>`), so each run overwrites the same rows (blank when fewer).
"""
from lib.skills import SKILLS

PER_CATEGORY = 3
TOP = 10


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


def build(entries, updated_at, per=PER_CATEGORY, top=TOP):
    """entries: [(row, features)] of recent winners / rising Shorts. -> Examples tab records."""
    ranked = sorted(entries, key=lambda e: (_num(e[0].get("breakout")), _num(e[0].get("views"))), reverse=True)

    def rec(cat, rank, have, item):
        r, f = item if item else ({}, {})
        return {"example_id": f"{cat}#{rank}", "category": cat, "rank": rank,
                "have_skill": "" if item is None else ("yes" if have else "no"),
                "url": r.get("url", ""), "title": r.get("title", ""), "channel_title": r.get("channel_title", ""),
                "views": r.get("views", ""), "breakout": r.get("breakout", ""),
                "age_days": r.get("age_days", ""), "style": describe(f) if item else "",
                "updated_at": updated_at}

    out = []
    best = ranked[:top]
    for i in range(top):
        out.append(rec("TOP breakouts (any style)", i + 1, True, best[i] if i < len(best) else None))
    for key, (name, test, have) in SKILLS.items():
        hits = [e for e in ranked if test(e[1])][:per]
        for i in range(per):
            out.append(rec(name, i + 1, have, hits[i] if i < len(hits) else None))
    return out
