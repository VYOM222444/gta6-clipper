"""Smart crop (follow the action) + streamer facecam detection, for 9:16 reframing.

Uses ffmpeg to read small grayscale frames (no extra install) and OpenCV's built-in face
detector (Colab has opencv preinstalled) only for the facecam. Everything is best-effort: if
anything fails the Renderer falls back to the plain centre crop.

- action path: every `step` seconds, where in the frame things move most (frame differences,
  facecam excluded), smoothed and speed-limited so the crop glides instead of jumping.
- facecam: a face found in the same corner spot in most sampled frames; its webcam box is found
  from the overlay's hard edges in the AVERAGE frame (gameplay moves and blurs out, the overlay
  border stays sharp), else estimated from the face size.
"""
import subprocess

import numpy as np


def read_gray(path, fps, width, start=0.0, duration=None):
    """-> (frames float32 [n, h, w], fps). Small grayscale frames via ffmpeg."""
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-i", str(path)]
    if duration:
        cmd += ["-t", f"{duration:.2f}"]
    cmd += ["-vf", f"fps={fps},scale={width}:-2", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    p = subprocess.run(cmd, capture_output=True, timeout=300)
    probe = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf",
                            f"scale={width}:-2", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                           capture_output=True, timeout=60)
    h = len(probe.stdout) // width if probe.stdout else 0
    if not h or not p.stdout:
        return np.zeros((0, 1, 1), np.float32), fps
    n = len(p.stdout) // (width * h)
    return np.frombuffer(p.stdout[: n * width * h], np.uint8).reshape(n, h, width).astype(np.float32), fps


# --- action path --------------------------------------------------------------------------------

def motion_points(frames, fps, exclude=None, min_strength=2.0):
    """[(t, cx 0..1 or None)] from consecutive frame differences. exclude = (x, y, w, h) in 0..1."""
    out = []
    if len(frames) < 2:
        return out
    n, h, w = frames.shape
    mask = np.ones((h, w), np.float32)
    if exclude:
        x, y, bw, bh = exclude
        mask[int(y * h): int((y + bh) * h) + 1, int(x * w): int((x + bw) * w) + 1] = 0
    for i in range(1, n):
        d = np.abs(frames[i] - frames[i - 1]) * mask
        if d.mean() < min_strength:          # nothing moving: keep the previous position
            out.append((i / fps, None))
            continue
        col = d.sum(axis=0)
        col = np.clip(col - np.median(col), 0, None)
        if col.sum() <= 0:
            out.append((i / fps, None))
            continue
        cx = float((col * np.arange(w)).sum() / col.sum()) / w
        out.append((i / fps, cx))
    return out


def smooth_path(points, max_speed=0.3, window=5, start=0.5):
    """Hold through gaps, median filter, then limit how fast the centre may move (per second)."""
    if not points:
        return []
    xs, last = [], start
    for _, cx in points:
        last = cx if cx is not None else last
        xs.append(last)
    half = window // 2
    med = [float(np.median(xs[max(0, i - half): i + half + 1])) for i in range(len(xs))]
    out, cur, prev_t = [], med[0], points[0][0]
    for (t, _), target in zip(points, med):
        step = max_speed * max(t - prev_t, 1e-6)
        cur = cur + max(-step, min(step, target - cur))
        out.append((round(t, 2), cur))
        prev_t = t
    return out


def crop_x_expr(path, scaled_w, crop_w, every=1.0, avoid=None):
    """ffmpeg expression of t for crop x (pixels) following `path` [(t, cx 0..1)], piecewise
    linear, flat sum (no deep nesting, works on old ffmpeg). None when a fixed x is fine."""
    if scaled_w <= crop_w or not path:
        return None
    lo, lim = 0, scaled_w - crop_w
    if avoid:   # (x, w) fractions to keep out of the crop (the facecam when it has its own band)
        ax0, ax1 = avoid[0] * scaled_w, (avoid[0] + avoid[1]) * scaled_w
        if avoid[0] + avoid[1] / 2 > 0.5 and ax0 - crop_w >= 0:
            lim = min(lim, ax0 - crop_w)
        elif avoid[0] + avoid[1] / 2 <= 0.5 and ax1 <= scaled_w - crop_w:
            lo = max(lo, ax1)

    def x_at(cx):
        return int(round(min(max(cx * scaled_w - crop_w / 2, lo), lim)))

    pts, next_t = [], -1.0
    for t, cx in path:
        if t >= next_t:
            pts.append((t, x_at(cx)))
            next_t = t + every
    if pts[-1][0] != path[-1][0]:
        pts.append((path[-1][0], x_at(path[-1][1])))
    if len({x for _, x in pts}) == 1:
        return str(pts[0][1])
    terms = [f"lt(t,{pts[0][0]})*{pts[0][1]}"]
    for (a, xa), (b, xb) in zip(pts, pts[1:]):
        if b <= a:
            continue
        terms.append(f"gte(t,{a})*lt(t,{b})*({xa}+{(xb - xa) / (b - a):.3f}*(t-{a}))")
    terms.append(f"gte(t,{pts[-1][0]})*{pts[-1][1]}")
    return "+".join(terms)


# --- facecam --------------------------------------------------------------------------------------

def detect_faces(frames):
    """[(frame_index, x, y, w, h)] with OpenCV's frontal-face Haar cascade (pixels)."""
    import os
    import cv2
    xml = os.environ.get("CLIPPER_HAAR_XML") or (getattr(getattr(cv2, "data", None), "haarcascades", "")
                                                 + "haarcascade_frontalface_default.xml")
    cascade = cv2.CascadeClassifier(xml)
    if cascade.empty():
        raise RuntimeError(f"face model not found ({xml})")
    found = []
    for i, f in enumerate(frames):
        for (x, y, w, h) in cascade.detectMultiScale(f.astype(np.uint8), scaleFactor=1.1, minNeighbors=5,
                                                      minSize=(16, 16)):
            found.append((i, int(x), int(y), int(w), int(h)))
    return found


def stable_face(faces, n_frames, w, h, min_share=0.5, grid=0.08, max_size=0.25):
    """The face that sits in the same off-centre spot in most frames (a facecam), or None."""
    groups = {}
    for i, x, y, fw, fh in faces:
        if fw > max_size * w:
            continue
        cx, cy = (x + fw / 2) / w, (y + fh / 2) / h
        key = (round(cx / grid), round(cy / grid))
        groups.setdefault(key, []).append((i, x, y, fw, fh))
    best = None
    for items in groups.values():
        frames_hit = len({i for i, *_ in items})
        if frames_hit >= max(2, min_share * n_frames) and (best is None or frames_hit > best[0]):
            best = (frames_hit, items)
    if not best:
        return None
    items = best[1]
    x, y, fw, fh = (float(np.median([it[k] for it in items])) for k in range(1, 5))
    cx, cy = (x + fw / 2) / w, (y + fh / 2) / h
    if 0.3 < cx < 0.7 and 0.3 < cy < 0.7:   # a face in the middle is gameplay, not a facecam
        return None
    return x, y, fw, fh


def _edge(profile, lo, hi, prefer_far=False):
    lo, hi = max(0, int(lo)), min(len(profile) - 1, int(hi))
    if hi <= lo:
        return None
    seg = profile[lo: hi + 1]
    k = int(np.argmax(seg))
    return lo + k if seg[k] > 3 * (np.median(profile) + 1e-3) else None


def webcam_box(avg, face):
    """Webcam rectangle around `face` (x, y, w, h pixels) from the average frame's sharp edges."""
    h, w = avg.shape
    fx, fy, fw, fh = face
    gx = np.abs(np.diff(avg, axis=1))
    gy = np.abs(np.diff(avg, axis=0))
    r0, r1 = int(max(0, fy - fh)), int(min(h - 1, fy + 2 * fh))
    colp = gx[r0:r1].mean(axis=0)
    left = _edge(colp, fx - 6 * fw, fx - 0.4 * fw)
    right = _edge(colp, fx + 1.4 * fw, fx + 7 * fw)
    left = 0 if left is None and fx < 3 * fw else left
    right = w - 1 if right is None and w - (fx + fw) < 3 * fw else right
    if left is not None and right is not None:
        rowp = gy[:, int(left): int(right) + 1].mean(axis=1)
        top = _edge(rowp, fy - 4 * fh, fy - 0.3 * fh)
        bottom = _edge(rowp, fy + 1.3 * fh, fy + 6 * fh)
        top = 0 if top is None and fy < 2.5 * fh else top
        bottom = h - 1 if bottom is None and h - (fy + fh) < 2.5 * fh else bottom
        if top is not None and bottom is not None:
            bw, bh = right - left + 1, bottom - top + 1
            if 0.9 <= bw / bh <= 2.6 and bw >= 2 * fw and bh >= 1.5 * fh:
                return float(left), float(top), float(bw), float(bh)
    # fallback: a 16:9 box ~4 face-widths wide around the face (face in the upper middle)
    bw = min(4.2 * fw, w)
    bh = min(bw * 9 / 16, h)
    bx = min(max(fx + fw / 2 - bw / 2, 0), w - bw)
    by = min(max(fy + fh * 0.6 - bh * 0.45, 0), h - bh)
    return float(bx), float(by), float(bw), float(bh)


def find_facecam(path, duration, samples=8, width=640, inset=0.04):
    """Facecam box as fractions (x, y, w, h) of the source frame, or None."""
    fps = max(samples / max(duration, 1.0), 0.2)
    frames, _ = read_gray(path, fps, width)
    if len(frames) < 3:
        return None
    n, h, w = frames.shape
    face = stable_face(detect_faces(frames), n, w, h)
    if not face:
        return None
    bx, by, bw, bh = webcam_box(frames.mean(axis=0), face)
    # step inside the overlay so its border / frame never shows
    ix, iy = bw * inset, bh * inset
    bx, by, bw, bh = bx + ix, by + iy, bw - 2 * ix, bh - 2 * iy
    return (bx / w, by / h, bw / w, bh / h)


def analyze(path, duration, cfg):
    """-> {"facecam": box or None, "path": [(t, cx)]} for one clip. Never raises."""
    out = {"facecam": None, "path": []}
    try:
        if cfg.get("facecam", True):
            out["facecam"] = find_facecam(path, duration, cfg.get("face_samples", 8), inset=cfg.get("cam_inset", 0.04))
    except Exception as e:  # no OpenCV, odd video: no facecam
        print(f"  note: facecam check skipped ({str(e)[:80]})")
    try:
        frames, fps = read_gray(path, cfg.get("motion_fps", 4), cfg.get("motion_width", 320))
        pts = motion_points(frames, fps, out["facecam"], cfg.get("min_motion", 2.0))
        out["path"] = smooth_path(pts, cfg.get("max_speed", 0.3), cfg.get("smooth_window", 5))
    except Exception as e:
        print(f"  note: smart crop skipped ({str(e)[:80]})")
    return out
