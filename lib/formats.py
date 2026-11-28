"""What kind of Short a researched video is (Researcher tag `format`), and which group it belongs to.

Owner (27 Nov): an editing trick that works on stream clips may not work on other kinds of Shorts
(skits, music edits, comparisons...), so everything used to BUILD or CHOOSE editing skills (viral
tricks, Examples tab, gap report, style, story shape, length) is computed from the group our own clips
belong to: `settings.researcher.skill_group` ("stream"). Shorts tagged before `format` existed fall
back to a guess: a visible facecam / split-screen facecam = stream clip.
"""

FORMATS = ["stream_clip", "gameplay_clip", "comparison", "edit_montage", "skit_irl", "news_talk", "other"]
GROUPS = {"stream": {"stream_clip"}}

PROMPT_LINE = (
    "- format: what kind of Short this is. stream_clip = a moment clipped from a livestream or a\n"
    "  creator's gameplay video where the streamer reacts (face or voice); gameplay_clip = only game\n"
    "  footage, nobody reacting; comparison = side by side / X vs Y; edit_montage = music edit,\n"
    "  cinematic or montage; skit_irl = real people acting or real-life footage; news_talk = someone\n"
    "  talking about news, leaks or tips; other.")


def group_of(features):
    """'stream' or 'other' for one Short's tags."""
    f = features or {}
    fmt = f.get("format")
    if fmt:
        return next((g for g, fs in GROUPS.items() if fmt in fs), "other")
    if f.get("facecam") is True or f.get("layout") == "split_screen_facecam":
        return "stream"
    return "other"
