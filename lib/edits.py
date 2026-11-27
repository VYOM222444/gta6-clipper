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
        # everything after the frozen frame plays f seconds later
        p["whooshes"] = [round(t + f, 2) if t > payoff else t for t in p["whooshes"]]
        p["impacts"] = [round(t + f, 2) if t > payoff + 0.05 else t for t in p["impacts"]]
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
    if p["payoff"] is not None and p["end"] < p.get("source_end", p["end"] + 1):
        out.append("cut")
    if remove_music:
        out.append("nomusic")
    return out


# --- ffmpeg pieces ------------------------------------------------------------------------------

def loop_cmd(src, dst, duration, d=0.3, has_audio=True):
    """Seamless loop ending (last pass): the clip starts d seconds in, and its last d seconds
    cross-fade (picture + sound) into the clip's first d seconds. The final frame is then the
    same frame the Short opens on, so the replay shows no jump. Length: duration - d.
    The file is opened twice (split + acrossfade on one input gives no audio in ffmpeg).
    Basic filters only (xfade/acrossfade exist in Colab's ffmpeg 4.4)."""
    body = round(duration - d, 3)
    fc = (f"[0:v]trim=start={d},setpts=PTS-STARTPTS[mv];[1:v]trim=0:{d},setpts=PTS-STARTPTS[hv];"
          f"[mv][hv]xfade=transition=fade:duration={d}:offset={round(body - d, 3)},format=yuv420p[v]")
    maps = ["-map", "[v]"]
    if has_audio:
        fc += (f";[0:a]atrim=start={d},asetpts=PTS-STARTPTS[mav];"
               f"[1:a]atrim=0:{round(d + 0.1, 3)},asetpts=PTS-STARTPTS[hav];"
               f"[mav][hav]acrossfade=d={d},atrim=0:{body},asetpts=PTS-STARTPTS[a]")
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    return ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-i", str(src), "-filter_complex", fc, *maps,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart", str(dst)]


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
    if freeze:  # hold the payoff frame (sound paused) then carry on
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
    for name, idx, times, vol in (("i", 1, p["impacts"], cfg.get("impact_volume", 0.9)),
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
