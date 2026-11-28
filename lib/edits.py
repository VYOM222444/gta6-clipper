"""Editing pack: punch-in zooms, sound effects, slow-mo replay, end right after the payoff,
optional music removal. Which effects are on comes from the Researcher's findings in the
Playbook tab ("right now" first, then 7-day), falling back to `settings.render.edits`.

Pure logic here (tested); render_batch runs the ffmpeg commands.

- analyze(audio): payoff second = loudest ~0.5 s after the first second; hits = sudden
  loudness jumps (crashes, gunshots, screams). Zooms land on these hits, so the cuts follow
  the clip's own rhythm (music is added later in the apps, so there is no beat to sync to).
- Sound effects are generated with ffmpeg (no downloaded files, no copyright).
"""
import hashlib
import re

SR = 16000
FRAME = 0.05  # seconds per loudness frame

FLAG_KEYS = {
    # flag: (playbook keys, kind)
    "zoom": (["editing.zoom_ins"], "median"),
    "impact": (["sound.sfx_impact"], "share"),
    "whoosh": (["sound.sfx_whoosh"], "share"),
    "replay": (["editing.replay_of_moment", "editing.slow_motion"], "share"),
    "end_after_payoff": (["editing.ends_right_after_payoff"], "share"),
}
MEDIAN_RE = re.compile(r"winners ([\d.]+) vs average ([\d.]+)")


# --- audio analysis ---------------------------------------------------------------------------

def loudness(audio, sr=SR):
    import numpy as np
    n = int(sr * FRAME)
    k = len(audio) // n
    if k == 0:
        return np.zeros(0)
    frames = audio[: k * n].reshape(k, n)
    return np.sqrt((frames.astype("float64") ** 2).mean(axis=1))


def analyze(audio, sr=SR, max_hits=4, min_gap=1.5):
    """-> (payoff_second or None, [hit seconds])"""
    import numpy as np
    rms = loudness(audio, sr)
    if len(rms) < int(2 / FRAME) or rms.max() <= 1e-4:
        return None, []
    win = max(1, int(0.5 / FRAME))
    smooth = np.convolve(rms, np.ones(win) / win, mode="same")
    lo, hi = int(1.0 / FRAME), len(rms) - int(0.3 / FRAME)
    payoff = (lo + int(np.argmax(smooth[lo:hi]))) * FRAME if hi > lo else None
    base = float(np.median(rms)) + 1e-6
    jump = np.diff(rms, prepend=rms[0])
    cands = [i for i in range(1, len(rms) - 1)
             if rms[i] > 2.5 * base and jump[i] > 1.5 * base and rms[i] >= rms[i - 1] and rms[i] >= rms[i + 1]]
    cands.sort(key=lambda i: -rms[i])
    hits = []
    for i in cands:
        t = round(i * FRAME, 2)
        if t >= 0.5 and all(abs(t - h) >= min_gap for h in hits):
            hits.append(t)
        if len(hits) >= max_hits:
            break
    return (round(payoff, 2) if payoff is not None else None), sorted(hits)


# --- which effects: Playbook findings -> flags --------------------------------------------------

def _verdict(row, kind):
    rule = str(row.get("rule", ""))
    if kind == "median":
        m = MEDIAN_RE.search(rule) or MEDIAN_RE.search(str(row.get("value", "")))
        if not m:
            return None
        w, a = float(m.group(1)), float(m.group(2))
        return w >= 1 and w > a
    if rule.startswith("DO"):
        return True
    if rule.startswith("AVOID"):
        return False
    return None


def flags_from_playbook(rows, defaults):
    """'Right now' findings win, then 7-day ones, then the defaults. Returns (flags, sources)."""
    by_id = {str(r.get("rule_id", "")): r for r in rows}
    flags, src = {}, {}
    for flag, (keys, kind) in FLAG_KEYS.items():
        flags[flag], src[flag] = bool(defaults.get(flag, False)), "default"
        for prefix, name in (("research.now.", "now"), ("research.", "7-day")):
            votes = [_verdict(by_id[prefix + k], kind) for k in keys if prefix + k in by_id]
            votes = [v for v in votes if v is not None]
            if votes:
                flags[flag], src[flag] = any(votes), name
                break
    return flags, src


# --- the plan for one clip ----------------------------------------------------------------------

def plan(duration, payoff, hits, flags, cfg):
    """Times are seconds in the rendered clip. replay = (start, end) of the source part replayed
    in slow motion after the clip."""
    end = duration
    if flags.get("end_after_payoff") and payoff is not None:
        end = min(duration, max(payoff + cfg.get("tail_seconds", 1.5), cfg.get("min_seconds", 8)))
    zooms = []
    if flags.get("zoom"):
        pts = sorted(set([h for h in hits if h < end - 0.5] + ([payoff] if payoff is not None and payoff < end else [])))
        for t in pts:
            if all(abs(t - z) >= 1.2 for z in zooms):
                zooms.append(t)
        zooms = zooms[: cfg.get("max_zooms", 4)]
    replay = None
    if flags.get("replay") and payoff is not None:
        a = max(0.0, payoff - cfg.get("replay_before", 1.2))
        b = min(end, payoff + cfg.get("replay_after", 0.8))
        if b - a >= 0.8:
            replay = (round(a, 2), round(b, 2))
    impacts = []
    if flags.get("impact") and payoff is not None and payoff < end:
        impacts.append(payoff)
        if replay:
            impacts.append(round(end + (payoff - replay[0]) / cfg.get("replay_speed", 0.5), 2))
    whooshes = []
    if flags.get("whoosh"):
        whooshes = [max(0.0, round(z - 0.25, 2)) for z in zooms if payoff is None or abs(z - payoff) > 0.05]
        if replay:
            whooshes.append(max(0.0, round(end - 0.3, 2)))
    p = {"end": round(end, 2), "zooms": zooms, "replay": replay, "impacts": impacts,
         "whooshes": whooshes, "payoff": payoff, "shake": None, "freeze": None}
    if flags.get("shake") and payoff is not None and payoff < end - 0.2:
        p["shake"] = (round(payoff, 2), round(min(end, payoff + cfg.get("shake_seconds", 0.4)), 2))
    # loop ending: not with a slow-mo replay (the clip would loop into the replay, not the action)
    p["loop"] = bool(flags.get("loop")) and not p["replay"] and end >= 4
    if flags.get("freeze") and payoff is not None and 0.3 < payoff < end - 0.3:
        f = cfg.get("freeze_seconds", 0.6)
        p["freeze"] = (round(payoff, 2), f)
        if cfg.get("freeze_style", "punch") == "punch":
            # punch freeze: the frozen frame covers the next f seconds while the sound plays on (no
            # silence, timeline unchanged); the shake comes right after it instead of under it
            f = min(f, round(end - payoff - 0.2, 2))
            p["freeze"] = (round(payoff, 2), f)
            if p["shake"]:
                a = round(payoff + f, 2)
                p["shake"] = (a, round(min(end, a + cfg.get("shake_seconds", 0.4)), 2)) if a < end - 0.2 else None
        else:
            # hold: everything after the frozen frame plays f seconds later
            p["whooshes"] = [round(t + f, 2) if t > payoff else t for t in p["whooshes"]]
            p["impacts"] = [round(t + f, 2) if t > payoff + 0.05 else t for t in p["impacts"]]
    return p


def dead_air(rms, payoff=None, cfg=None, frame=FRAME):
    """Dead-air cutter (28 Nov): winning Shorts have no dead air, ours are raw stream cuts. A pause =
    loudness more than `deadair_drop_db` below the clip's loud level (90th percentile) for at least
    `deadair_min_gap` s. Each cut keeps `deadair_pad` s of the pause on both sides, never touches the
    build-up before the payoff (`deadair_protect` s) or the half second after it, leaves a pause at
    the very end alone (the end cut / loop handle the end), removes at most `deadair_max_share` of the
    clip and keeps it >= min_seconds. -> [(start, end)] seconds to KEEP, or [] = nothing worth cutting."""
    import numpy as np
    cfg = cfg or {}
    n = len(rms)
    if n < int(3 / frame):
        return []
    db = 20 * np.log10(np.maximum(np.asarray(rms, dtype="float64"), 1e-6))
    quiet = db < float(np.percentile(db, 90)) - cfg.get("deadair_drop_db", 20)
    dur, pad = n * frame, cfg.get("deadair_pad", 0.15)
    zone = (payoff - cfg.get("deadair_protect", 2.0), payoff + 0.5) if payoff is not None else None
    cuts, i = [], 0
    while i < n:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < n and quiet[j]:
            j += 1
        a, b = i * frame, j * frame
        i = j
        if b - a < cfg.get("deadair_min_gap", 0.6) or j >= n:   # short, or the clip's last pause
            continue
        a, b = (0.0 if a == 0 else a + pad), b - pad
        parts = [(a, b)]
        if zone and a < zone[1] and b > zone[0]:     # keep the build-up before the payoff
            parts = [(a, min(b, zone[0])), (max(a, zone[1]), b)]
        cuts += [(round(x, 2), round(y, 2)) for x, y in parts if y - x >= 0.2]
    budget, total, chosen = cfg.get("deadair_max_share", 0.3) * dur, 0.0, []
    for a, b in sorted(cuts, key=lambda c: c[0] - c[1]):          # longest pauses first
        if total + (b - a) <= budget and dur - total - (b - a) >= cfg.get("min_seconds", 8):
            chosen.append((a, b))
            total += b - a
    if not chosen:
        return []
    keep, t = [], 0.0
    for a, b in sorted(chosen):
        if a - t >= 0.1:
            keep.append((round(t, 2), round(a, 2)))
        t = b
    if dur - t >= 0.1:
        keep.append((round(t, 2), round(dur, 2)))
    return keep


def word_gaps(words, rms, payoff=None, cfg=None, frame=FRAME):
    """Dead-air cutter v2 (28 Nov, from 6 reference stream clips: a cut every ~0.8-1 s, no gap >= 0.3 s
    left): cut the gaps between Whisper words that are >= `deadair_gap` s, keeping `deadair_word_pad` s
    on each side. A gap where the GAME is loud (its loudest 10% within `deadair_game_db` dB of the clip's
    loud level: crash, explosion) is kept. The silence before the first word is cut too; the gap after
    the last word is left alone (end cut / loop). Same payoff protection, budget (`deadair_max_share`)
    and min_seconds as dead_air(). No words (no speech) -> loudness dead_air(). -> [(start, end)] to
    KEEP, or [] = nothing worth cutting."""
    import numpy as np
    cfg = cfg or {}
    words = [w for w in (words or []) if w.get("end", 0) > w.get("start", 0)]
    if len(words) < 2:
        return dead_air(rms, payoff, cfg, frame)
    db = 20 * np.log10(np.maximum(np.asarray(rms, dtype="float64"), 1e-6))
    dur = len(db) * frame
    loud = float(np.percentile(db, 90))
    gap_min, pad = cfg.get("deadair_gap", 0.3), cfg.get("deadair_word_pad", 0.08)
    zone = (payoff - cfg.get("deadair_protect", 2.0), payoff + 0.5) if payoff is not None else None
    gaps = [(0.0, words[0]["start"], True)] + [(a["end"], b["start"], False) for a, b in zip(words, words[1:])]
    cuts = []
    for a, b, first in gaps:
        if b - a < gap_min:
            continue
        seg = db[int(a / frame):max(int(b / frame), int(a / frame) + 1)]
        if seg.size and float(np.percentile(seg, 90)) > loud - cfg.get("deadair_game_db", 6):
            continue                                       # loud game moment: keep it
        a, b = (0.0 if first else a + pad), b - pad
        parts = [(a, b)]
        if zone and a < zone[1] and b > zone[0]:
            parts = [(a, min(b, zone[0])), (max(a, zone[1]), b)]
        cuts += [(round(x, 2), round(y, 2)) for x, y in parts if y - x >= 0.1]
    budget, total, chosen = cfg.get("deadair_max_share", 0.4) * dur, 0.0, []
    for a, b in sorted(cuts, key=lambda c: c[0] - c[1]):              # longest gaps first
        if total + (b - a) <= budget and dur - total - (b - a) >= cfg.get("min_seconds", 8):
            chosen.append((a, b))
            total += b - a
    if not chosen:
        return []
    keep, t = [], 0.0
    for a, b in sorted(chosen):
        if a - t >= 0.05:
            keep.append((round(t, 2), round(a, 2)))
        t = b
    if dur - t >= 0.05:
        keep.append((round(t, 2), round(dur, 2)))
    return keep


def shift_words(words, keep):
    """Move Whisper word times onto the cut clip (no second Whisper run). A word whose middle falls in
    a removed part is dropped; times are clamped into the kept part they belong to."""
    out, offs, t = [], [], 0.0
    for a, b in keep:
        offs.append((a, b, t - a))
        t += b - a
    for w in words:
        mid = (w["start"] + w["end"]) / 2
        part = next((p for p in offs if p[0] <= mid < p[1]), None)
        if part:
            a, b, d = part
            out.append({**w, "start": round(max(w["start"], a) + d, 3), "end": round(min(w["end"], b) + d, 3)})
    return out


def deadair_cmd(src, dst, keep, has_audio=True, fade=0.015):
    """Jump-cut the source down to the `keep` parts (basic filters only: split, trim, concat; old
    ffmpeg safe). Every part's sound gets a 15 ms fade in/out so the cuts never click."""
    n = len(keep)
    fc = []
    if n > 1:
        fc.append("[0:v]split=" + str(n) + "".join(f"[vs{i}]" for i in range(n)))
        if has_audio:
            fc.append("[0:a]asplit=" + str(n) + "".join(f"[as{i}]" for i in range(n)))
    pads = ""
    for i, (a, b) in enumerate(keep):
        fc.append(f"{f'[vs{i}]' if n > 1 else '[0:v]'}trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}]")
        pads += f"[v{i}]"
        if has_audio:
            out = round(max(b - a - fade, 0), 3)
            fc.append(f"{f'[as{i}]' if n > 1 else '[0:a]'}atrim=start={a}:end={b},asetpts=PTS-STARTPTS,"
                      f"afade=t=in:d={fade},afade=t=out:st={out}:d={fade}[a{i}]")
            pads += f"[a{i}]"
    fc.append(f"{pads}concat=n={n}:v=1:a={1 if has_audio else 0}[v]" + ("[a]" if has_audio else ""))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-filter_complex", ";".join(fc),
           "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    return cmd + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(dst)]


FAIL_WORDS = ("fail", "wasted", "busted", "oops", "bruh", "worst", "dumb", "glitch", "bug", "gone wrong", "funny",
              "lol", "clown", "noob", "rip ", "broke", "💀", "😂", "🤣", "😭")
PAYOFF_SOUND = {"tip": "ding", "secret": "ding", "news": "ding"}


def payoff_sound(row):
    """Meme sound + ding (28 Nov): 'ding' for a clever reveal (tip / secret / news clips), 'boom' (deep
    meme-style boom) for a fail or a funny beat in a chaos / reaction clip (from its hook, title and
    description words; the Busted Again brand is fails by design), None = keep the normal impact."""
    ct = str(row.get("content_type", "")).strip().lower()
    if ct in PAYOFF_SOUND:
        return PAYOFF_SOUND[ct]
    text = " ".join(str(row.get(k, "")) for k in ("hook_text", "title_label", "title", "description")).lower()
    if ct in ("chaos", "reaction") and (row.get("brand") == "busted_again" or any(w in text for w in FAIL_WORDS)):
        return "boom"
    return None


def meme_sfx_cmds(boom_path, ding_path):
    """Generate the boom + ding once (48 kHz stereo wav), like sfx_cmds: no files, no copyright."""
    boom = ("aevalsrc='0.95*sin(2*PI*(58-18*t)*t)*exp(-2.4*t)+0.35*sin(2*PI*(116-36*t)*t)*exp(-3.5*t)'"
            ":s=48000:d=1.3,lowpass=f=900,aformat=channel_layouts=stereo")
    ding = ("aevalsrc='0.55*sin(2*PI*1568*t)*exp(-5*t)+0.3*sin(2*PI*2349*t)*exp(-7*t)"
            "+0.12*sin(2*PI*3136*t)*exp(-9*t)':s=48000:d=1.1,aformat=channel_layouts=stereo")
    return [["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", boom, str(boom_path)],
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", ding, str(ding_path)]]


def apply_payoff_sound(p, kind, cfg):
    """The payoff gets `kind` (boom / ding) instead of the impact sound (one sound, not two); if the
    impact is off it gets one anyway. Returns the plan."""
    if not kind or p.get("payoff") is None or p["payoff"] >= p["end"]:
        return p
    p["payoff_sfx"] = kind
    p["impact_volume"] = cfg.get(f"{kind}_volume", 0.9 if kind == "boom" else 0.6)
    if not p["impacts"]:
        p["impacts"] = [p["payoff"]]
    return p


def experiment_flags(flags, batch_id, experiments):
    """Skills under test go on a stable random share of clips (same clip = same answer on re-runs),
    so our own posts can be compared with vs without them."""
    out = dict(flags)
    for skill, share in (experiments or {}).items():
        h = int(hashlib.sha1(f"{batch_id}:{skill}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        out[skill] = h < float(share)
    return out


def tags(p, remove_music=False):
    out = []
    if p["zooms"]:
        out.append(f"zoom{len(p['zooms'])}")
    if p["impacts"]:
        out.append("impact")
    if p["whooshes"]:
        out.append("whoosh")
    if p["replay"]:
        out.append("replay")
    if p.get("shake"):
        out.append("shake")
    if p.get("freeze"):
        out.append("freeze")
    if p.get("loop"):
        out.append("loop")
    if p.get("deadair"):
        out.append(f"deadair{p['deadair'][0]}")
    if p.get("payoff_sfx"):
        out.append(p["payoff_sfx"])
    if p["payoff"] is not None and p["end"] < p.get("source_end", p["end"] + 1):
        out.append("cut")
    if remove_music:
        out.append("nomusic")
    return out


# --- ffmpeg pieces ------------------------------------------------------------------------------

def loop_points(frames, fps, lo, hi, max_start=0.5, min_len=4.0):
    """Loop ending like viral Shorts (studied 27 Nov): a HARD cut where the last frame looks like
    the first. frames = small grey frames of the finished clip at `fps`. The end is searched in
    [lo, hi] seconds (just after the payoff, owner's choice: never run on past it); the start may
    move up to `max_start` s in. -> (start_s, end_s, jump) with the smallest picture jump, or None."""
    n = len(frames)
    if n < 2:
        return None
    s_max = min(int(max_start * fps), n // 4)
    e_lo = max(int(round(lo * fps)) - 1, int(min_len * fps), 0)
    e_hi = min(int(round(hi * fps)), n)
    best = None
    for s in range(0, s_max + 1):
        for e in range(max(e_lo, s + int(min_len * fps)), e_hi):
            jump = float(abs(frames[e] - frames[s]).mean())
            if best is None or jump < best[2] - 1e-6:
                best = (s, e, jump)
    if best is None:
        return None
    s, e, jump = best
    return round(s / fps, 3), round((e + 1) / fps, 3), round(jump, 2)


def loop_cmd(src, dst, start, end, has_audio=True, fade=0.03):
    """Cut the finished clip to [start, end): the replay then jumps from the last frame straight
    to a frame that looks like it (hard cut, no cross-fade: a fade ghosts both pictures). Sound gets
    a tiny 30 ms fade at both ends so the cut never clicks. Basic filters only."""
    length = round(end - start, 3)
    fc = f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS,format=yuv420p[v]"
    maps = ["-map", "[v]"]
    if has_audio:
        fc += (f";[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS,"
               f"afade=t=in:d={fade},afade=t=out:st={round(length - fade, 3)}:d={fade}[a]")
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    return ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", fc, *maps,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart", str(dst)]


def loop_window(p, duration, tail=2.0):
    """Where a looped clip may end, in seconds of the finished clip: from just after the payoff
    (+ freeze) to `tail` s later, so it always 'ends right after the payoff' (owner chose this over
    running on until the scene looks like the start, 27 Nov). No payoff known -> the last `tail` s."""
    if p.get("payoff") is None:
        return round(max(0.0, duration - tail), 2), round(duration, 2)
    lo = p["payoff"] + (p["freeze"][1] if p.get("freeze") else 0) + 0.3   # after the freeze too
    return round(lo, 2), round(min(duration, lo + tail), 2)


def zoom_chain(src, dst, zooms, w, h, amount=0.12, hold=0.7):
    """Punch-in zooms with only basic filters (old ffmpeg, e.g. Colab's 4.4, has no time variable
    in scale): a constant zoomed-in copy is overlaid on the video during each zoom window.
    Returns a filter-graph piece `[src] ... [dst]`, or "" when there are no zooms."""
    if not zooms:
        return ""
    zw, zh = int(round(w * (1 + amount) / 2) * 2), int(round(h * (1 + amount) / 2) * 2)
    on = "+".join(f"between(t,{z:.2f},{z + hold:.2f})" for z in zooms)
    return (f"[{src}]split=2[{dst}a][{dst}b];[{dst}b]scale={zw}:{zh},crop={w}:{h}[{dst}z];"
            f"[{dst}a][{dst}z]overlay=0:0:enable='{on}'[{dst}]")


def sfx_cmds(impact_path, whoosh_path):
    """Generate the two effect sounds once (48 kHz stereo wav)."""
    impact = ("aevalsrc='0.9*sin(2*PI*(70-45*t)*t)*exp(-7*t)+0.25*(random(0)-0.5)*exp(-30*t)'"
              ":s=48000:d=0.7,aformat=channel_layouts=stereo")
    whoosh = ("anoisesrc=d=0.55:c=pink:r=48000:a=0.6,highpass=f=400,lowpass=f=6000,"
              "afade=t=in:d=0.35,afade=t=out:st=0.35:d=0.2,aformat=channel_layouts=stereo")
    return [["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", impact, str(impact_path)],
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", whoosh, str(whoosh_path)]]


def pass2_cmd(main, out, p, impact_path, whoosh_path, cfg):
    """Second pass on the finished vertical clip: slow-mo replay after it + sound effects.
    Returns None when there is nothing to do."""
    shake, freeze = p.get("shake"), p.get("freeze")
    if not (p["replay"] or p["impacts"] or p["whooshes"] or shake or freeze):
        return None
    speed = cfg.get("replay_speed", 0.5)
    reencode = bool(p["replay"] or shake or freeze)
    fc, vlabel, alabel = [], "[0:v]", "[0:a]"
    if p["replay"]:   # replay is cut from the untouched clip, before shake/freeze change the timing
        fc += ["[0:v]split=2[m0][r0]", "[0:a]asplit=2[ma0][ra0]"]
        mv, ma = "[m0]", "[ma0]"
    else:
        mv, ma = "[0:v]", "[0:a]"
    if shake:   # jittered, slightly zoomed copy over the frame for a moment (basic filters only)
        a, b = shake
        amp = cfg.get("shake_pixels", 18)
        fc += [f"{mv}split=2[sb][sj0]",
               f"[sj0]crop=iw-{4 * amp}:ih-{4 * amp}:x='{2 * amp}+{amp}*sin(t*83)':y='{2 * amp}+{amp}*cos(t*67)',"
               f"scale=1080:1920,setsar=1[sj]",
               f"[sb][sj]overlay=0:0:enable='between(t,{a},{b})'[shk]"]
        mv = "[shk]"
    if freeze and cfg.get("freeze_style", "punch") == "punch":
        # The payoff frame, zoomed in a bit with a short white flash, covers the next f seconds while
        # the sound keeps playing: reads as a deliberate freeze, not as the video lagging (owner, 27 Nov:
        # the old silent hold of the whole frame looked like a lag).
        t, f = freeze
        top, bh = p.get("freeze_box") or (0, 1920)   # only the gameplay: a split facecam keeps reacting
        z = cfg.get("freeze_zoom", 1.08)
        zw, zh = int(round(1080 * z / 2) * 2), int(round(bh * z / 2) * 2)
        fc += [f"{mv}split=2[fzm][fz0]",
               f"[fz0]trim=start={t}:duration=0.04,setpts=PTS-STARTPTS,crop=1080:{bh}:0:{top},"
               f"scale={zw}:{zh},crop=1080:{bh},"
               f"tpad=stop_mode=clone:stop_duration={f},setpts=PTS+{t}/TB[fzf]",
               f"[fzm][fzf]overlay=0:{top}:eof_action=pass,"
               f"drawbox=x=0:y={top}:w=iw:h={bh}:color=white@0.55:t=fill:enable='between(t,{t},{round(t + 0.07, 2)})'[frz]"]
        mv = "[frz]"
    elif freeze:  # hold the payoff frame (sound paused) then carry on
        t, f = freeze
        fc += [f"{mv}split=3[fa0][fb0][fc0]",
               f"[fa0]trim=0:{t},setpts=PTS-STARTPTS[fa]",
               f"[fb0]trim=start={t}:duration=0.04,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={f}[fb]",
               f"[fc0]trim=start={t + 0.04},setpts=PTS-STARTPTS[fc]",
               f"{ma}asplit=2[xa0][xc0]",
               f"[xa0]atrim=0:{t},asetpts=PTS-STARTPTS[xa]",
               f"anullsrc=r=48000:cl=stereo,atrim=0:{f + 0.04},asetpts=PTS-STARTPTS[xb]",
               f"[xc0]atrim=start={t + 0.04},asetpts=PTS-STARTPTS[xc]",
               "[xa]aresample=48000,aformat=channel_layouts=stereo[xa1]",
               "[xc]aresample=48000,aformat=channel_layouts=stereo[xc1]",
               "[fa][xa1][fb][xb][fc][xc1]concat=n=3:v=1:a=1[frz][frza]"]
        mv, ma = "[frz]", "[frza]"
    if p["replay"]:
        a, b = p["replay"]
        fc += [f"[r0]trim={a}:{b},setpts=(PTS-STARTPTS)/{speed}[r]",
               f"[ra0]atrim={a}:{b},asetpts=PTS-STARTPTS,atempo={speed},aresample=48000,"
               f"aformat=channel_layouts=stereo[ra]",
               f"{ma}aresample=48000,aformat=channel_layouts=stereo[ma1]",
               f"{mv}[ma1][r][ra]concat=n=2:v=1:a=1[v][a0]"]
        vlabel, alabel = "[v]", "[a0]"
    else:
        vlabel, alabel = mv, ma
    mix = [alabel]
    for name, idx, times, vol in (("i", 1, p["impacts"], p.get("impact_volume", cfg.get("impact_volume", 0.9))),
                                  ("w", 2, p["whooshes"], cfg.get("whoosh_volume", 0.5))):
        if not times:
            continue
        fc.append(f"[{idx}:a]asplit={len(times)}" + "".join(f"[{name}{k}s]" for k in range(len(times))))
        for k, t in enumerate(times):
            ms = int(t * 1000)
            fc.append(f"[{name}{k}s]adelay={ms}|{ms},volume={vol}[{name}{k}]")
            mix.append(f"[{name}{k}]")
    if len(mix) > 1:
        fc.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0,alimiter=limit=0.95[a]")
        alabel = "[a]"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(main),
           "-i", str(impact_path), "-i", str(whoosh_path), "-filter_complex", ";".join(fc),
           "-map", vlabel if reencode else "0:v", "-map", alabel]
    cmd += (["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"] if reencode
            else ["-c:v", "copy"])
    cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(out)]
    return cmd


def demucs_cmd(src_audio, out_dir):
    """Split voice from everything else (music + game sound). Colab only; slow on CPU."""
    return ["python", "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs", "-o", str(out_dir), str(src_audio)]


def remix_cmd(src_video, vocals, rest, out, rest_volume=0.3):
    """Keep the voice, turn the rest (music, game sound) down; video untouched."""
    return ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src_video), "-i", str(vocals),
            "-i", str(rest), "-filter_complex",
            f"[2:a]volume={rest_volume}[r];[1:a][r]amix=inputs=2:duration=first:normalize=0[a]",
            "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", str(out)]
