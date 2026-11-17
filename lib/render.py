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
        out.append({"words": [x["word"] for x in c], "start": start, "end": max(end, start + 0.25)})
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


def layout_boxes(src_w, src_h, tpl):
    """Where the foreground video sits on the canvas: (fg_w, fg_h, top_y)."""
    if src_h > src_w:  # vertical source: fill the whole canvas
        return W, H, 0
    zoom = tpl.get("fg_zoom", 1.0)
    scaled_w = int(round(W * zoom / 2) * 2)
    fg_h = int(round(scaled_w * src_h / src_w / 2) * 2)
    fg_h = min(fg_h, H)
    return W, fg_h, (H - fg_h) // 2


def build_ass(tpl, chunks, title_label, hook_text, credit, duration, src_w=1920, src_h=1080):
    fonts, acc = tpl["fonts"], tpl["accent"]
    lab, hk, cap, cr = tpl["label"], tpl["hook"], tpl["caption"], tpl["credit"]
    _, fg_h, top = layout_boxes(src_w, src_h, tpl)
    vertical = top == 0
    label_y = lab["y"] if not vertical else 300
    cap_y = min(cap["y"], SAFE_BOTTOM - cap["size"]) if not vertical else SAFE_BOTTOM - 160
    credit_y = top + fg_h + 28 if not vertical else SAFE_BOTTOM - 40
    # hook sits ABOVE the title label, never on the gameplay (the payoff happens in the
    # first seconds, right where a centred hook would cover it)
    hook_y = hk.get("y", 250) if not vertical else hk.get("y_vertical", 480)

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Label,{fonts['label']},{lab['size']},{ass_color(lab['text'])},&H000000FF,{ass_color(acc)},{ass_color(acc)},0,0,0,0,100,100,1,0,3,{lab['box_pad']},0,5,40,40,0,1
Style: Hook,{fonts['hook']},{hk['size']},{ass_color(hk['color'])},&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,{hk['outline']},3,5,60,60,0,1
Style: Caption,{fonts['caption']},{cap['size']},{ass_color(cap['color'])},&H000000FF,&H00000000,&H78000000,0,0,0,0,100,100,1,0,1,{cap['outline']},{cap['shadow']},5,70,140,0,1
Style: Credit,{fonts['credit']},{cr['size']},{ass_color(cr['color'], 1 - cr['alpha'])},&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,0,4,{cr['margin_x']},40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    end = ass_time(duration)
    lines = []
    if title_label:
        lines.append(f"Dialogue: 2,{ass_time(0)},{end},Label,,0,0,0,,{{\\pos({W // 2},{label_y})}}{ass_text(title_label.upper())}")
    if hook_text:
        hook_end = min(hk["seconds"], duration)
        lines.append(f"Dialogue: 3,{ass_time(0)},{ass_time(hook_end)},Hook,,0,0,0,,"
                     f"{{\\pos({W // 2},{hook_y})\\fad(0,200)}}{ass_text(hook_text.upper())}")
    if credit:
        lines.append(f"Dialogue: 1,{ass_time(0)},{end},Credit,,0,0,0,,{{\\pos({cr['margin_x']},{credit_y})}}"
                     f"via {ass_text(credit)}")
    hl = ass_color(acc)
    white = ass_color(cap["color"])
    for c in chunks:
        if c["start"] >= duration:
            break
        words = [w.upper() if cap.get("uppercase") else w for w in c["words"]]
        k = keyword_index(c["words"])
        parts = []
        for i, w in enumerate(words):
            parts.append(f"{{\\c{hl}}}{w}{{\\c{white}}}" if i == k else w)
        lines.append(f"Dialogue: 0,{ass_time(c['start'])},{ass_time(min(c['end'], duration))},Caption,,0,0,0,,"
                     f"{{\\pos({W // 2 - 35},{cap_y})}}{' '.join(parts)}")
    return head + "\n".join(lines) + "\n"


# --- ffmpeg ----------------------------------------------------------------------------------

def _filter_path(p):
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def ffmpeg_cmd(src, ass_path, fonts_dir, out, tpl, src_w, src_h, duration, has_audio=True):
    fg_w, fg_h, top = layout_boxes(src_w, src_h, tpl)
    ass = f"ass='{_filter_path(ass_path)}':fontsdir='{_filter_path(fonts_dir)}'"
    if top == 0:
        vf = (f"[0:v]fps=30,scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"setsar=1,{ass}[v]")
    else:
        zoom_w = int(round(W * tpl.get("fg_zoom", 1.0) / 2) * 2)
        vf = (f"[0:v]fps=30,split=2[a][b];"
              f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"boxblur={tpl['bg_blur']}:2,eq=brightness={tpl['bg_brightness']}[bg];"
              f"[b]scale={zoom_w}:{fg_h},crop={W}:{fg_h}[fg];"
              f"[bg][fg]overlay=0:{top},setsar=1,{ass}[v]")
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
