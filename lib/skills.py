"""Gap report: editing tricks winning Shorts use that our Renderer can or can't do yet.

The Researcher already tags every analysed Short (blind Gemini tags). For each trick we compare
how many winners vs average Shorts use it and look it up in SKILLS (what the Renderer can do).
Result -> Playbook rows `gaps.<key>`, ranked: the top "BUILD NEXT" rows are the next editing
skills to build (the owner reviews a sample of each before it ships).

When a new skill is built: set its `have` to True here (and wire it in lib/edits.py / lib/style.py).
"""

# key -> (name, test on a feature dict, can the Renderer do it?)
SKILLS = {
    "zoom_ins": ("zoom punch-ins", lambda f: (f.get("zoom_ins") or 0) > 0, True),
    "sfx_impact": ("impact sound", lambda f: "impact" in (f.get("sound_effects") or []), True),
    "sfx_whoosh": ("whoosh sound", lambda f: "whoosh" in (f.get("sound_effects") or []), True),
    "replay_of_moment": ("slow-mo replay", lambda f: f.get("replay_of_moment") is True, True),
    "ends_right_after_payoff": ("ends right after the payoff", lambda f: f.get("ends_right_after_payoff") is True, True),
    "has_title_label": ("title label", lambda f: f.get("has_title_label") is True, True),
    "text_hook": ("big text hook", lambda f: f.get("hook_type") in ("text_question", "text_statement")
                  or f.get("hook_text_kind") not in (None, "", "none"), True),
    "captions": ("captions", lambda f: f.get("caption_style") not in (None, "", "none"), True),
    "caption_keyword_color": ("coloured caption keyword", lambda f: f.get("caption_keyword_color") is True, True),
    "layout_blur": ("blurred-background layout", lambda f: f.get("layout") == "blurred_background", True),
    "layout_fill": ("full-screen crop", lambda f: f.get("layout") == "full_vertical", True),
    "layout_black": ("black bars", lambda f: f.get("layout") == "black_bars", True),
    "screen_shake": ("screen shake", lambda f: f.get("screen_shake") is True, True),      # experiment
    "freeze_frame": ("freeze frame", lambda f: f.get("freeze_frame") is True, True),      # experiment
    # not built yet
    "split_screen_facecam": ("split-screen facecam", lambda f: f.get("layout") == "split_screen_facecam", True),  # experiment
    "slow_motion": ("slow motion inside the clip", lambda f: f.get("slow_motion") is True, False),
    "emoji_or_stickers": ("emoji / sticker pop-ups", lambda f: f.get("emoji_or_stickers") is True, False),
    "loop_ending": ("loop ending", lambda f: f.get("loop_ending") is True, True),
    "sfx_riser": ("riser sound", lambda f: "riser" in (f.get("sound_effects") or []), False),
    "sfx_record_scratch": ("record-scratch sound", lambda f: "record_scratch" in (f.get("sound_effects") or []), False),
    "sfx_meme_sound": ("meme sounds", lambda f: "meme_sound" in (f.get("sound_effects") or []), False),
    "sfx_ding": ("ding sound", lambda f: "ding" in (f.get("sound_effects") or []), False),
    "fast_cuts": ("fast cuts (8+ cuts)", lambda f: (f.get("cuts") or 0) >= 8, False),
    "cuts_on_beat": ("cuts on the beat", lambda f: f.get("cuts_on_beat") is True, False),
    "voiceover": ("voiceover narration", lambda f: f.get("voiceover") is True, False),
}
# What a raw moment needs so a trick works well on it. The Watcher is told the top viral tricks
# (Playbook style.viral_tricks) with these needs and rates each moment's `trick_fit` 1-10.
FIT_NEEDS = {
    "zoom_ins": "sudden hits or big moments to punch in on",
    "sfx_impact": "one sharp hit (crash, explosion, fall, punch) for an impact sound",
    "sfx_whoosh": "fast movement or a quick change of scene",
    "replay_of_moment": "a fast payoff that is worth seeing again in slow motion",
    "ends_right_after_payoff": "one clear payoff the clip can end on right away",
    "has_title_label": "a moment that can be named in 2-5 words",
    "text_hook": "a moment that can be teased in a few words before it happens",
    "captions": "clear speech (streamer talking or yelling)",
    "caption_keyword_color": "clear speech with one key word to highlight",
    "layout_blur": "action that needs the whole wide frame",
    "layout_fill": "action that stays near the centre of the screen",
    "layout_black": "action that needs the whole wide frame",
    "screen_shake": "one violent hit (crash, explosion, big fall) to shake the screen on",
    "freeze_frame": "one peak instant worth freezing (mid-air, impact, shocked face)",
    "split_screen_facecam": "a visible facecam with a big reaction (scream, laugh, shock)",
    "slow_motion": "a short spectacular action worth slowing down",
    "emoji_or_stickers": "clear emotions or funny beats to pop an emoji on",
    "loop_ending": "an ending that looks like the opening (same place, same action), so the replay feels seamless",
    "sfx_riser": "tension building up before the payoff",
    "sfx_record_scratch": "a sudden 'wait, what?' stop",
    "sfx_meme_sound": "a fail or awkward beat that fits a meme sound",
    "sfx_ding": "a clever or perfect move",
    "fast_cuts": "many short beats in a row",
    "cuts_on_beat": "many short beats in a row",
    "voiceover": "something that needs explaining (a secret, tip or news)",
}


def fit_line(tricks):
    """'1. screen shake (needs ...); 2. ...' for the Watcher prompt; '' when no tricks."""
    return "\n".join(f"{i}. {t['name']}: needs {FIT_NEEDS.get(t['key'], 'a moment it suits')}"
                     for i, t in enumerate(tricks or [], start=1))


# never built on purpose (our rules): music is added inside the apps
OUT_OF_SCOPE = {"background_music": ("background music (added in the apps)", lambda f: f.get("background_music") is True)}


def _share(items, test):
    return sum(1 for x in items if test(x)) / len(items) if items else 0.0


def verdict(have, w, a, min_diff=0.15, min_use=0.2):
    if have:
        return "HAVE IT"
    if w - a >= min_diff and w >= min_use:
        return "BUILD NEXT"
    if w - a >= min_diff / 2 and w >= min_use / 2:
        return "MAYBE"
    return "SKIP"


ORDER = {"BUILD NEXT": 0, "MAYBE": 1, "HAVE IT": 2, "SKIP": 3, "NOT FOR US": 4}


def report(win, avg, min_diff=0.15, min_use=0.2, min_winners=8):
    """-> list of dicts sorted: BUILD NEXT (biggest gap first), MAYBE, HAVE IT, SKIP."""
    if len(win) < min_winners or not avg:
        return []
    out = []
    for key, (name, test, have) in SKILLS.items():
        w, a = _share(win, test), _share(avg, test)
        out.append({"key": key, "name": name, "w": w, "a": a, "have": have,
                    "verdict": verdict(have, w, a, min_diff, min_use)})
    for key, (name, test) in OUT_OF_SCOPE.items():
        w, a = _share(win, test), _share(avg, test)
        out.append({"key": key, "name": name, "w": w, "a": a, "have": False, "verdict": "NOT FOR US"})
    out.sort(key=lambda g: (ORDER[g["verdict"]], -(g["w"] - g["a"])))
    return out


def playbook_rows(gaps, source, updated_at):
    rows = []
    for rank, g in enumerate(gaps, start=1):
        can = "we can do it" if g["have"] else "we can't do it yet"
        rows.append({"rule_id": f"gaps.{g['key']}", "section": "gaps",
                     "rule": f"#{rank} {g['verdict']}: {g['name']} - winners {round(g['w'] * 100)}% vs "
                             f"average {round(g['a'] * 100)}% ({can})",
                     "value": f"{g['w'] - g['a']:+.2f}", "source": source, "updated_at": updated_at})
    return rows
