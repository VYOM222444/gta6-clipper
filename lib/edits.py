"""Editing pack: punch-in zooms, sound effects, slow-mo replay, end right after the payoff,
optional music removal. Which effects are on comes from the Researcher's findings in the
Playbook tab ("right now" first, then 7-day), falling back to `settings.render.edits`.

Pure logic here (tested); render_batch runs the ffmpeg commands.

- analyze(audio): payoff second = loudest ~0.5 s after the first second; hits = sudden
  loudness jumps (crashes, gunshots, screams). Zooms land on these hits, so the cuts follow
  the clip's own rhythm (music is added later in the apps, so there is no beat to sync to).
- Sound effects are generated with ffmpeg (no downloaded files, no copyright).
"""
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
    return {"end": round(end, 2), "zooms": zooms, "replay": replay, "impacts": impacts,
            "whooshes": whooshes, "payoff": payoff}


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
    if p["payoff"] is not None and p["end"] < p.get("source_end", p["end"] + 1):
        out.append("cut")
    if remove_music:
        out.append("nomusic")
    return out


# --- ffmpeg pieces ------------------------------------------------------------------------------

def zoom_expr(zooms, amount=0.12, hold=0.7):
    """Scale factor as an ffmpeg expression of t: a quick punch-in at each zoom time."""
    if not zooms:
        return None
    terms = "+".join(f"between(t,{z:.2f},{z + hold:.2f})" for z in zooms)
    return f"(1+{amount}*min(1,{terms}))"


def zoom_filter(expr, w, h):
    """Scale up by expr each frame, then crop back to w x h around the centre."""
    return (f"scale=w='trunc({w}*{expr}/2)*2':h='trunc({h}*{expr}/2)*2':eval=frame,"
            f"crop={w}:{h}")


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
    if not (p["replay"] or p["impacts"] or p["whooshes"]):
        return None
    speed = cfg.get("replay_speed", 0.5)
    fc, vlabel, alabel = [], "[0:v]", "[0:a]"
    if p["replay"]:
        a, b = p["replay"]
        fc += [f"[0:v]split=2[m][r0]", f"[r0]trim={a}:{b},setpts=(PTS-STARTPTS)/{speed}[r]",
               f"[0:a]asplit=2[ma][ra0]", f"[ra0]atrim={a}:{b},asetpts=PTS-STARTPTS,atempo={speed}[ra]",
               "[m][ma][r][ra]concat=n=2:v=1:a=1[v][a0]"]
        vlabel, alabel = "[v]", "[a0]"
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
           "-map", vlabel if p["replay"] else "0:v", "-map", alabel]
    cmd += (["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"] if p["replay"]
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
