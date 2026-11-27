"""Pure helpers for the Renderer: caption chunks, keyword highlights, the .ass subtitle
file (title label, hook, captions, credit) and the ffmpeg command. No I/O here, so it
can be tested without video files.

Canvas: 1080x1920 (9:16). Safe zone: nothing important in the bottom ~20% (y > 1536) or
in the right-hand app buttons.
"""
import json
import re

from lib.config import REPO_ROOT

W, H = 1080, 1920
SAFE_BOTTOM = int(H * 0.8)
STOPWORDS = {
    "the", "and", "that", "this", "with", "from", "they", "them", "what", "when", "your",
    "have", "just", "like", "there", "their", "about", "would", "could", "into", "were",
    "been", "then", "than", "it's", "i'm", "don't", "gonna", "really", "yeah", "okay",
}


def load_template(brand):
    with open(REPO_ROOT / "templates" / f"{brand}.json", encoding="utf-8") as f:
        return json.load(f)


# --- captions --------------------------------------------------------------------------

def clean_word(w):
    return w.strip().replace("{", "(").replace("}", ")").replace("\\", "/")


def chunk_words(words, max_words=3, pause_split=0.45):
    """words: [{word, start, end}] -> [{words: [...], start, end}] for on-screen chunks.
    Breaks on max_words, on a pause, or after sentence punctuation."""
    chunks, cur = [], []
    for w in words:
        text = clean_word(w["word"])
        if not text:
            continue
        if cur and (len(cur) >= max_words or w["start"] - cur[-1]["end"] > pause_split
                    or re.search(r"[.!?]$", cur[-1]["word"])):
            chunks.append(cur)
            cur = []
        cur.append({"word": text, "start": float(w["start"]), "end": float(w["end"])})
    if cur:
        chunks.append(cur)
    out = []
    for i, c in enumerate(chunks):
        start = c[0]["start"]
        end = chunks[i + 1][0]["start"] if i + 1 < len(chunks) else c[-1]["end"] + 0.3
        end = min(end, c[-1]["end"] + 0.6)          # don't hang on screen during silence
        times = []
        for j, x in enumerate(c):   # per-word on-screen time: until the next word starts
            nxt = c[j + 1]["start"] if j + 1 < len(c) else max(end, start + 0.25)
            times.append((x["start"], max(nxt, x["start"] + 0.12)))
        out.append({"words": [x["word"] for x in c], "start": start, "end": max(end, start + 0.25),
                    "times": times})
    return out


def keyword_index(words):
    """Index of the one word to highlight in a chunk, or None."""
    best, best_score = None, 0
    for i, w in enumerate(words):
        core = re.sub(r"[^\w]", "", w).lower()
        if not core or core in STOPWORDS:
            continue
        score = len(core)
        if any(ch.isdigit() for ch in core):
            score += 6
        if w.isupper() and len(core) > 1:
            score += 4
        if w.endswith("!"):
            score += 3
        if score > best_score:
            best, best_score = i, score
    return best if best_score >= 5 else None


# --- .ass file ----------------------------------------------------------------------------

def ass_color(hex_rgb, alpha=0.0):
    """'FF3B30' -> '&H00303BFF' (ASS is AABBGGRR, alpha 00 = opaque)."""
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    a = f"{int(round(alpha * 255)):02X}"
    return f"&H{a}{b}{g}{r}".upper()


def ass_time(t):
    t = max(t, 0)
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_text(text):
    return clean_word(text).replace("\n", "\\N")


def mask_words(words, bad):
    """Mask strong profanity / slurs in caption words: 'fucking' -> 'f******'.
    A list entry matches the whole word; an entry ending in '*' also matches longer words."""
    exact = {b.lower() for b in bad if not b.endswith("*")}
    prefix = tuple(b[:-1].lower() for b in bad if b.endswith("*"))
    out = []
    for w in words:
        core = re.sub(r"[^\w]", "", w["word"].lower())
        if core and (core in exact or (prefix and core.startswith(prefix))):
            first = next((ch for ch in w["word"] if ch.isalnum()), "*")
            w = {**w, "word": first + "*" * max(len(core) - 1, 2)}
        out.append(w)
    return out


def layout_boxes(src_w, src_h, tpl):
    """Where the foreground video sits on the canvas: (fg_w, fg_h, top_y).
    tpl['layout'] == 'fill' (or a vertical source) fills the whole canvas (centre crop)."""
    if tpl.get("layout") == "split" and tpl.get("split_cam_h"):
        ch = int(tpl["split_cam_h"])
        return W, H - ch, ch                       # facecam on top, gameplay below
    if src_h > src_w or tpl.get("layout") == "fill":
        return W, H, 0
    zoom = tpl.get("fg_zoom", 1.0) if tpl.get("layout") != "black" else 1.0
    scaled_w = int(round(W * zoom / 2) * 2)
    fg_h = int(round(scaled_w * src_h / src_w / 2) * 2)
    fg_h = min(fg_h, H)
    return W, fg_h, (H - fg_h) // 2


def split_cam_h(facecam, src_w, src_h, lo=0.25, hi=0.42):
    """Height of the facecam band on top of a split-screen (keeps the webcam's shape, clamped)."""
    fx, fy, fw, fh = facecam
    ar = (fw * src_w) / max(fh * src_h, 1)
    return int(round(min(max(W / ar, lo * H), hi * H) / 2) * 2)


DEFAULT_STYLE = {"title_label": True, "hook_text": True, "captions": "phrase", "caption_position": "bottom",
                 "caption_keyword_color": True, "layout": "blur"}
POP = r"\fscx72\fscy72\t(0,90,\fscx108\fscy108)\t(90,160,\fscx100\fscy100)"
MAX_TEXT_W = 940


def fit_size(text, size, max_w=MAX_TEXT_W, char_w=0.5):
    """Font size so one line of `text` fits max_w px (Anton is ~0.5 x size per character)."""
    n = max(len(text), 1)
    return max(48, min(size, int(max_w / (n * char_w))))


def caption_y(position, fg_h, top, size):
    """Caption centre line. middle = lower part of the gameplay; bottom = just under it."""
    if position == "top":
        return max(top + int(size * 0.9), 560)
    if position == "middle":
        return top + int(fg_h * (0.72 if top else 0.62))
    y = top + fg_h + 70 + int(size * 0.55) if top else SAFE_BOTTOM - 140   # under the "via" credit
    return min(y, SAFE_BOTTOM - size)


def caption_events(chunks, style, cap, hl, white, duration):
    """Caption Dialogue texts: [(start, end, text)]. word_by_word shows one word at a time."""
    out = []
    for c in chunks:
        if c["start"] >= duration:
            break
        words = [w.upper() if cap.get("uppercase") else w for w in c["words"]]
        if style.get("captions") == "word_by_word" and c.get("times"):
            for w, (a, b) in zip(words, c["times"]):
                if a < duration:
                    out.append((a, min(b, duration), [w], None))
            continue
        k = keyword_index(c["words"]) if style.get("caption_keyword_color") else None
        out.append((c["start"], min(c["end"], duration), words, k))
    lines = []
    for a, b, words, k in out:
        text = " ".join(words)
        fs = fit_size(text, cap["size"])
        parts = [f"{{\\c{hl}}}{w}{{\\c{white}}}" if i == k else w for i, w in enumerate(words)]
        lines.append((a, b, f"{{\\fs{fs}{POP}}}" + " ".join(parts)))
    return lines


def hook_pos(tpl, src_w, src_h, style=None):
    """y (centre) of the hook text: ABOVE the gameplay (or high up on a full-screen clip), never on
    the payoff; on the seam of a split-screen."""
    st = dict(DEFAULT_STYLE, **(style or {}))
    hk = tpl["hook"]
    _, fg_h, top = layout_boxes(src_w, src_h, tpl)
    vertical = top == 0
    if tpl.get("layout") == "split" and top:
        return top                              # on the seam between facecam and gameplay
    y = hk.get("y", 250) if not vertical else hk.get("y_vertical", 480)
    if not st.get("title_label") and not vertical:
        y = max(y, top - int(hk["size"] * 0.9))
    return y


def freeze_box(tpl, src_w, src_h, style=None, hook=True):
    """(top, height) of the part a freeze frame may hold: the gameplay (a split facecam keeps
    reacting), starting below the hook text so the text never jumps."""
    _, fg_h, top = layout_boxes(src_w, src_h, tpl)
    if tpl.get("layout") != "split":
        top, fg_h = 0, H
    st = dict(DEFAULT_STYLE, **(style or {}))
    if hook and st.get("hook_text"):
        below = hook_pos(tpl, src_w, src_h, style) + int(tpl["hook"]["size"] * 0.75)
        if below > top:
            fg_h, top = fg_h - (below - top), below
    fg_h -= fg_h % 2
    return top + top % 2, fg_h


def build_ass(tpl, chunks, title_label, hook_text, credit, duration, src_w=1920, src_h=1080, style=None,
              hook_full=False):
    """style (lib/style.py): which elements are shown and how. None = the old fixed look.
    hook_full: the hook stays on screen the whole clip, with no pop-in (looping clips: viral loops
    keep their top text all the way, so the replay shows no text popping in again)."""
    st = dict(DEFAULT_STYLE, **(style or {}))
    fonts, acc = tpl["fonts"], tpl["accent"]
    lab, hk, cap, cr = tpl["label"], tpl["hook"], tpl["caption"], tpl["credit"]
    _, fg_h, top = layout_boxes(src_w, src_h, tpl)
    vertical = top == 0
    split = tpl.get("layout") == "split" and bool(top)
    label_y = lab["y"] if not vertical else 300
    cap_y = caption_y(st.get("caption_position", "bottom"), fg_h, top, cap["size"])
    credit_y = top + fg_h + 28 if not vertical else SAFE_BOTTOM - 40
    if split:   # gameplay reaches the bottom: credit and bottom captions sit inside it
        credit_y = SAFE_BOTTOM - 40
        if st.get("caption_position", "bottom") == "bottom":
            cap_y = SAFE_BOTTOM - 150
    hook_y = hook_pos(tpl, src_w, src_h, st)

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Label,{fonts['label']},{lab['size']},{ass_color(lab['text'])},&H000000FF,{ass_color(acc)},{ass_color(acc)},0,0,0,0,100,100,1,0,3,{lab['box_pad']},0,5,40,40,0,1
Style: Hook,{fonts['hook']},{hk['size']},{ass_color(hk['color'])},&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,{hk['outline']},3,5,60,60,0,1
Style: Caption,{fonts['caption']},{cap['size']},{ass_color(cap['color'])},&H000000FF,&H00000000,&H78000000,0,0,0,0,100,100,1,0,1,{cap['outline']},{cap['shadow']},5,40,40,0,1
Style: Credit,{fonts['credit']},{cr['size']},{ass_color(cr['color'], 1 - cr['alpha'])},&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,0,4,{cr['margin_x']},40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    end = ass_time(duration)
    lines = []
    if title_label and st.get("title_label"):
        text = title_label.upper()
        lines.append(f"Dialogue: 2,{ass_time(0)},{end},Label,,0,0,0,,{{\\pos({W // 2},{label_y})"
                     f"\\fs{fit_size(text, lab['size'], 900, 0.46)}}}{ass_text(text)}")
    if hook_text and st.get("hook_text"):
        text = hook_text.upper()
        hook_end = duration if hook_full else min(hk["seconds"], duration)
        anim = "" if hook_full else f"{POP}\\fad(0,200)"
        lines.append(f"Dialogue: 3,{ass_time(0)},{ass_time(hook_end)},Hook,,0,0,0,,"
                     f"{{\\pos({W // 2},{hook_y})\\fs{fit_size(text, hk['size'], 980, 0.5)}{anim}}}"
                     f"{ass_text(text)}")
    if credit:
        lines.append(f"Dialogue: 1,{ass_time(0)},{end},Credit,,0,0,0,,{{\\pos({cr['margin_x']},{credit_y})}}"
                     f"via {ass_text(credit)}")
    if st.get("captions") != "none":
        for a, b, text in caption_events(chunks, st, cap, ass_color(acc), ass_color(cap["color"]), duration):
            lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Caption,,0,0,0,,"
                         f"{{\\pos({W // 2},{cap_y})}}{text}")
    return head + "\n".join(lines) + "\n"


# --- ffmpeg ----------------------------------------------------------------------------------

def _filter_path(p):
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _follow_crop(path, src_w, src_h, out_h, avoid=None):
    """crop filter that follows the action (lib/reframe path) after scaling the source to out_h."""
    from lib.reframe import crop_x_expr
    sw = int(round(src_w * out_h / src_h / 2) * 2)
    expr = crop_x_expr(path or [(0.0, 0.5)], sw, W, avoid=avoid) if (path or avoid) and src_w > src_h else None
    x = f":x='{expr}'" if expr else ""
    return f"scale=-2:{out_h},crop={W}:{out_h}{x}:y=0" if expr else \
        f"scale={W}:{out_h}:force_original_aspect_ratio=increase,crop={W}:{out_h}"


def ffmpeg_cmd(src, ass_path, fonts_dir, out, tpl, src_w, src_h, duration, has_audio=True, zoom=None,
               reframe=None):
    """zoom: optional (times, amount): punch-in zooms on the gameplay (lib/edits.zoom_chain).
    reframe: optional {"path": [(t, cx)], "facecam": (x, y, w, h) fractions} from lib/reframe:
    full-screen crops follow the action; layout 'split' puts the facecam on top."""
    from lib.edits import zoom_chain
    fg_w, fg_h, top = layout_boxes(src_w, src_h, tpl)
    ass = f"ass='{_filter_path(ass_path)}':fontsdir='{_filter_path(fonts_dir)}'"
    path = (reframe or {}).get("path") or None
    cam = (reframe or {}).get("facecam")
    if tpl.get("layout") == "split" and cam and top:
        fx, fy, fw, fh = cam
        zc = zoom_chain("g0", "g", zoom[0], W, fg_h, zoom[1]) if zoom and zoom[0] else ""
        vf = (f"[0:v]fps=30,split=2[c0][p0];"
              f"[c0]crop=iw*{fw:.4f}:ih*{fh:.4f}:iw*{fx:.4f}:ih*{fy:.4f},"
              f"scale={W}:{top}:force_original_aspect_ratio=increase,crop={W}:{top},setsar=1[cam];"
              f"[p0]{_follow_crop(path, src_w, src_h, fg_h, (fx, fw))},setsar=1[g0];"
              + (f"{zc};" if zc else "[g0]null[g];") +
              f"[cam][g]vstack=inputs=2,setsar=1,{ass}[v]")
    elif top == 0:
        crop = _follow_crop(path, src_w, src_h, H)
        zc = zoom_chain("g0", "g", zoom[0], W, H, zoom[1]) if zoom and zoom[0] else ""
        if zc:
            vf = f"[0:v]fps=30,{crop}[g0];{zc};[g]setsar=1,{ass}[v]"
        else:
            vf = f"[0:v]fps=30,{crop},setsar=1,{ass}[v]"
    else:
        zoom_w = W if tpl.get("layout") == "black" else int(round(W * tpl.get("fg_zoom", 1.0) / 2) * 2)
        vf = (f"[0:v]fps=30,split=2[a][b];"
              f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              + ("drawbox=x=0:y=0:w=iw:h=ih:c=black:t=fill[bg];" if tpl.get("layout") == "black" else
                 f"boxblur={tpl['bg_blur']}:2,eq=brightness={tpl['bg_brightness']}[bg];") +
              f"[b]scale={zoom_w}:{fg_h},crop={W}:{fg_h}[fg0];")
        zc = zoom_chain("fg0", "fg", zoom[0], W, fg_h, zoom[1]) if zoom and zoom[0] else ""
        vf += (zc + ";" if zc else "[fg0]null[fg];") + f"[bg][fg]overlay=0:{top},setsar=1,{ass}[v]"
    ld = tpl["loudness"]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
           "-filter_complex", vf, "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "0:a:0", "-af", f"loudnorm=I={ld['I']}:TP={ld['TP']}:LRA={ld['LRA']}",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-t", f"{duration:.2f}", str(out)]
    return cmd


def post_text(row):
    """The .txt saved next to each clip: what to paste when posting."""
    return (f"BRAND: {row.get('brand', '')}\n\n"
            f"TITLE:\n{row.get('title', '')}\n\n"
            f"DESCRIPTION:\n{row.get('description', '')}\n\n"
            f"HASHTAGS:\n{row.get('hashtags', '')}\n\n"
            f"INSTAGRAM / FACEBOOK CAPTION:\n{row.get('ig_caption', '')}\n\n"
            f"SOURCE: https://youtu.be/{row.get('video_id', '')}?t={row.get('start_s', '')}\n")
