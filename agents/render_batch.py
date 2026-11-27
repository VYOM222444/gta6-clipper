"""Fetcher + Renderer (runs in Colab: colab/render.ipynb).

For every Batch row with approved = yes that isn't rendered yet:
  1. Fetch: yt-dlp downloads ONLY that clip's section (start_s-end_s), max 1080p.
  2. Transcribe: faster-whisper word timestamps (model from Hugging Face; GPU if present).
  3. Render: 9:16 (blurred background + zoomed gameplay), brand template from
     templates/<brand>.json: top title label, hook text (first 2.5 s), word-chunk captions
     with keyword highlight, creator credit, loudness normalised.
  4. Save to Google Drive: <output_dir>/<date>/<brand>/<batch_id>.mp4 and .txt with
     title / description / hashtags to paste when posting.
Batch columns updated: fetch_status, render_status, output_path, rendered_at,
template_version. Safe to re-run: finished rows are skipped, failed rows are retried.

Run (Colab):  from agents import render_batch; render_batch.main()
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from lib import reframe
from lib import style as clip_style
from lib import edits, render, sheets
from lib.config import load_rules, load_settings
from lib.schema import TABS

TAB = "Batch"
YES = {"yes", "y", "✅", "ok", "true"}
FORMAT = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080][vcodec!=none]/bv*+ba/b[vcodec!=none]"


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _batch_day(row):
    try:
        return date.fromisoformat(str(row.get("batch_id"))[:10])
    except ValueError:
        return date.min


def to_render(rows, limit, max_age_days=None, today=None):
    """Approved, not-yet-rendered rows, NEWEST batch first (trends fade fast). Rows from batches
    older than max_age_days are skipped (stale moments are not worth a render or a post)."""
    today = today or datetime.now(timezone.utc).date()
    out = []
    for r in rows:
        if str(r.get("approved", "")).strip().lower() not in YES:
            continue
        if r.get("render_status") == "done" or not r.get("batch_id"):
            continue
        if max_age_days is not None:
            day = _batch_day(r)
            if day != date.min and (today - day).days > max_age_days:
                continue
        out.append(r)
    out.sort(key=_batch_day, reverse=True)  # newest day first; row order kept within a day (stable sort)
    return out[:limit]


def ytdlp_cmd(row, out_path, js_runtime=None):
    start, end = int(_f(row["start_s"])), int(_f(row["end_s"]))
    cmd = [sys.executable, "-m", "yt_dlp", "-f", FORMAT, "--download-sections", f"*{start}-{end}",
           "--force-keyframes-at-cuts", "--merge-output-format", "mp4", "--no-playlist",
           "--no-warnings", "--no-progress", "-o", str(out_path)]
    if js_runtime:
        cmd += ["--js-runtimes", js_runtime]
    cmd += cookie_args()
    cmd.append(f"https://www.youtube.com/watch?v={row['video_id']}")
    return cmd


BOT_CHECK = "confirm you"   # "Sign in to confirm you're not a bot"


def cookie_args():
    """--cookies <file> when YTDLP_COOKIES points to an existing cookies.txt (Colab copies it from Drive)."""
    path = os.environ.get("YTDLP_COOKIES", "")
    return ["--cookies", path] if path and os.path.exists(path) else []


def is_bot_block(text):
    return BOT_CHECK in (text or "") and "bot" in (text or "")


def output_paths(output_dir, row, day):
    folder = Path(output_dir) / day / row["brand"]
    return folder / f"{row['batch_id']}.mp4", folder / f"{row['batch_id']}.txt"


def words_from_segments(segments):
    words = []
    for seg in segments:
        for w in (getattr(seg, "words", None) or []):
            if w.word.strip():
                words.append({"word": w.word.strip(), "start": float(w.start), "end": float(w.end)})
    return words


def probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,width,height:format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    d = json.loads(p.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), {})
    has_audio = any(s["codec_type"] == "audio" for s in d["streams"])
    if not v:
        raise ValueError("source file has no video stream")
    return int(v["width"]), int(v["height"]), float(d["format"]["duration"]), has_audio


GPU_ERRORS = ("libcublas", "libcudnn", "cuda", "cublas", "cudnn", "ctranslate2")


def preload_cuda_libs():
    """Best effort: load the pip-installed CUDA 12 libraries (nvidia-cublas-cu12, nvidia-cudnn-cu12)
    so CTranslate2 finds libcublas.so.12 on Colab. Returns the libraries that were loaded."""
    import ctypes
    import glob
    import site
    loaded = []
    roots = list(site.getsitepackages()) + [site.getusersitepackages()]
    for name in ("cublas", "cudnn"):
        for root in roots:
            for so in sorted(glob.glob(f"{root}/nvidia/{name}/lib/lib*.so*")):
                try:
                    ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
                    loaded.append(Path(so).name)
                except OSError:
                    pass
    return loaded


def is_gpu_error(exc):
    return any(k in str(exc).lower() for k in GPU_ERRORS)


class Transcriber:
    """Whisper word timestamps. Uses the GPU when it works; if the GPU is missing a library
    (Colab: "libcublas.so.12 is not found") it switches to the CPU once and carries on."""

    def __init__(self, size):
        self.size = size
        try:
            import torch
            cuda = torch.cuda.is_available()
        except ImportError:
            cuda = False
        if cuda:
            try:
                print("CUDA libraries preloaded:", ", ".join(preload_cuda_libs()) or "none found")
            except Exception as e:  # never block the render on this
                print(f"note: CUDA preload skipped: {e}")
        self.device = "cuda" if cuda else "cpu"
        self.model = self._load(self.device)

    def _load(self, device):
        from faster_whisper import WhisperModel
        model = WhisperModel(self.size, device=device, compute_type="float16" if device == "cuda" else "int8")
        print(f"Whisper '{self.size}' on {'GPU' if device == 'cuda' else 'CPU'}")
        return model

    def _run(self, audio):
        segments, info = self.model.transcribe(audio, word_timestamps=True, vad_filter=True)
        return words_from_segments(list(segments)), info.language   # list(): errors surface here

    def words(self, path):
        # Decode with ffmpeg ourselves: faster-whisper's own decoder (PyAV) breaks when Colab
        # ships a newer PyAV ("open() got an unexpected keyword argument 'metadata_errors'").
        audio = load_audio(path)
        if audio.size == 0:
            return [], ""
        try:
            return self._run(audio)
        except Exception as e:
            if self.device != "cuda" or not is_gpu_error(e):
                raise
            print(f"GPU failed ({str(e)[:100]}); switching to CPU for the rest of this run.")
            self.device = "cpu"
            self.model = self._load("cpu")
            return self._run(audio)


def load_audio(path, sr=16000):
    """Audio as mono float32 numpy array at 16 kHz (what Whisper expects), via ffmpeg."""
    import numpy as np
    p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
                        "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(p.stdout, np.int16).astype(np.float32) / 32768.0


def read_playbook():
    try:
        return sheets.read_records(sheets.open_sheet(sheets.get_client(), load_settings()["sheet"]["id"]).worksheet("Playbook"))
    except Exception as e:  # no Playbook: defaults
        print(f"note: Playbook not read ({e}); using defaults")
        return None


def prepare_style(cfg, rows):
    """The clip style winners use (Playbook style.* rows, written by the Researcher)."""
    st, why, _ = clip_style.from_playbook(rows, cfg.get("style_defaults") or {})
    print("Style (from winners):")
    for k in ("captions", "caption_position", "caption_keyword_color", "hook_text", "title_label", "layout"):
        print(f"  {k} = {st.get(k)}  <- {why.get(k, 'default')[:120]}")
    return st


def prepare_edits(cfg, work, rows=None):
    """Effects for this run: Playbook findings > settings.render.edits. Generates the SFX once."""
    ecfg = dict(cfg.get("edits") or {})
    if not ecfg.get("enabled"):
        return None
    if rows is not None:
        flags, src = edits.flags_from_playbook(rows, ecfg)
    else:
        flags, src = {k: bool(ecfg.get(k)) for k in edits.FLAG_KEYS}, {}
    print("Edits:", ", ".join(f"{k}={'on' if v else 'off'} ({src.get(k, 'default')})" for k, v in flags.items()))
    work.mkdir(parents=True, exist_ok=True)
    ecfg["impact_path"], ecfg["whoosh_path"] = work / "sfx_impact.wav", work / "sfx_whoosh.wav"
    for cmd in edits.sfx_cmds(ecfg["impact_path"], ecfg["whoosh_path"]):
        subprocess.run(cmd, check=True, capture_output=True)
    ecfg["flags"] = flags
    return ecfg


def remove_music(src, work, ecfg):
    """Demucs: keep the voice, turn music + game sound down. Returns the new source or src."""
    wav, out_dir = work / f"{src.stem}.wav", work / "demucs"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "2", "-ar", "44100", str(wav)],
                   check=True, capture_output=True)
    p = subprocess.run(edits.demucs_cmd(wav, out_dir), capture_output=True, text=True)
    stem = out_dir / "htdemucs" / wav.stem
    if p.returncode != 0 or not (stem / "vocals.wav").exists():
        print(f"  note: music removal failed, original audio kept ({(p.stderr or '').strip()[-100:]})")
        return src
    out = work / f"{src.stem}.nomusic.mp4"
    subprocess.run(edits.remix_cmd(src, stem / "vocals.wav", stem / "no_vocals.wav", out,
                                   ecfg.get("rest_volume", 0.3)), check=True, capture_output=True)
    return out


def save_preview(video, ass_path, chunks, cfg, row, day):
    """Debug copy for checking a clip without opening it: one small frame (in the middle of the
    first caption, else at 3 s) + the subtitle file -> <output_dir>/_debug/<day>/. Never fails the clip."""
    try:
        dbg = Path(cfg["output_dir"]) / "_debug" / day
        dbg.mkdir(parents=True, exist_ok=True)
        t = (chunks[0]["start"] + chunks[0]["end"]) / 2 if chunks else 3.0
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(t, 0.1):.2f}", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=540:-2", str(dbg / f"{row['batch_id']}.jpg")],
                       capture_output=True, timeout=60)
        shutil.copy(str(ass_path), str(dbg / f"{row['batch_id']}.ass.txt"))
        print(f"  {row['batch_id']}: {sum(1 for c in chunks)} caption chunks; preview in _debug/{day}/")
    except Exception as e:
        print(f"  note: preview not saved ({e})")


def render_one(row, cfg, transcriber, day, js_runtime, ecfg=None, style=None):
    work = Path(cfg["work_dir"])
    work.mkdir(parents=True, exist_ok=True)
    src = work / f"{row['batch_id']}.src.mp4"
    for attempt in (1, 2):   # a cached/partial file without video is deleted and fetched again once
        if not src.exists():
            p = subprocess.run(ytdlp_cmd(row, src, js_runtime), capture_output=True, text=True)
            if p.returncode != 0 or not src.exists():
                msg = ((p.stderr or p.stdout).strip().splitlines() or ["download failed"])[-1]
                if is_bot_block(msg):
                    return {"blocked": True}
                return {"fetch_status": f"failed: {msg[:120]}"}
        try:
            w, h, dur, has_audio = probe(src)
            break
        except (ValueError, subprocess.CalledProcessError) as e:
            src.unlink(missing_ok=True)
            if attempt == 2:
                lf = subprocess.run([sys.executable, "-m", "yt_dlp", "-F", "--no-warnings",
                                     f"https://www.youtube.com/watch?v={row['video_id']}"],
                                    capture_output=True, text=True)
                vids = [l for l in lf.stdout.splitlines() if "video only" in l or " mp4 " in l]
                print(f"  {row['batch_id']}: no video in download; formats listed with video: {len(vids)}")
                print("   " + ((lf.stderr or lf.stdout).strip().splitlines() or ["(no output)"])[-1][:200])
                return {"fetch_status": f"failed: bad download ({e})"[:140]}
    plan, used = None, src
    if ecfg and has_audio and ecfg.get("remove_music"):
        used = remove_music(src, work, ecfg)
    if ecfg and has_audio:
        payoff, hits = edits.analyze(load_audio(used))
        flags = edits.experiment_flags(ecfg["flags"], row["batch_id"], ecfg.get("experiments"))
        plan = edits.plan(dur, payoff, hits, flags, ecfg)
        plan["source_end"] = dur
    words, lang = transcriber.words(used) if has_audio else ([], "")
    words = render.mask_words(words, load_rules().get("captions", {}).get("mask_words", []))
    tpl = render.load_template(row["brand"])
    if style and style.get("layout"):
        tpl["layout"] = style["layout"]
    rf, rf_tags = None, []
    rcfg = cfg.get("reframe") or {}
    if rcfg.get("enabled") and w > h:
        rf = reframe.analyze(used, dur, rcfg)
        if rf["facecam"] and edits.experiment_flags({}, row["batch_id"], {"split": rcfg.get("split_share", 0.5)})["split"]:
            tpl["layout"] = "split"
            tpl["split_cam_h"] = render.split_cam_h(rf["facecam"], w, h)
            rf_tags.append("split")
        if tpl.get("layout") in ("fill", "split") and rf["path"]:
            rf_tags.append("follow")
        print(f"  {row['batch_id']}: facecam={'yes' if rf['facecam'] else 'no'}, layout={tpl.get('layout')}")
    chunks = render.chunk_words(words, tpl["caption"]["max_words"], tpl["caption"]["pause_split"])
    ass_path = work / f"{row['batch_id']}.ass"
    ass_path.write_text(render.build_ass(tpl, chunks, row.get("title_label", ""), row.get("hook_text", ""),
                                         row.get("creator_credit", ""), dur, w, h, style), encoding="utf-8")
    mp4, txt = output_paths(cfg["output_dir"], row, day)
    mp4.parent.mkdir(parents=True, exist_ok=True)
    tmp = work / f"{row['batch_id']}.out.mp4"
    end = plan["end"] if plan else dur
    zoom = (plan["zooms"], ecfg.get("zoom_amount", 0.12)) if plan and plan["zooms"] else None
    p = subprocess.run(render.ffmpeg_cmd(used, ass_path, cfg["fonts_dir"], tmp, tpl, w, h, end, has_audio, zoom,
                                         rf), capture_output=True, text=True)
    if p.returncode != 0 and (zoom or rf):   # never lose a clip over a zoom or reframe: retry plain
        print(f"  note: zoom/reframe failed, retrying plain ({p.stderr.strip()[:200]})")
        if plan:
            plan["zooms"] = []
        if tpl.get("layout") == "split":
            tpl["layout"] = (style or {}).get("layout", "fill")
            ass_path.write_text(render.build_ass(tpl, chunks, row.get("title_label", ""), row.get("hook_text", ""),
                                                 row.get("creator_credit", ""), dur, w, h, style), encoding="utf-8")
        rf_tags = []
        p = subprocess.run(render.ffmpeg_cmd(used, ass_path, cfg["fonts_dir"], tmp, tpl, w, h, end, has_audio),
                           capture_output=True, text=True)
    if p.returncode != 0:
        err = p.stderr.strip()
        return {"fetch_status": "done", "render_status": f"failed: {err[:80]} ... {err[-100:]}"}
    edit_tags = []
    if plan:
        cmd = edits.pass2_cmd(tmp, work / f"{row['batch_id']}.fx.mp4", plan, ecfg["impact_path"],
                              ecfg["whoosh_path"], ecfg)
        if cmd:
            p2 = subprocess.run(cmd, capture_output=True, text=True)
            if p2.returncode == 0:
                shutil.move(str(work / f"{row['batch_id']}.fx.mp4"), str(tmp))
            else:
                print(f"  note: effects pass failed, plain clip kept ({p2.stderr.strip()[-100:]})")
                plan["replay"], plan["impacts"], plan["whooshes"] = None, [], []
        if plan.get("loop"):
            looped = work / f"{row['batch_id']}.loop.mp4"
            _, _, tdur, t_audio = probe(tmp)
            p3 = subprocess.run(edits.loop_cmd(tmp, looped, tdur, ecfg.get("loop_seconds", 0.3), t_audio),
                                capture_output=True, text=True)
            if p3.returncode == 0:
                shutil.move(str(looped), str(tmp))
            else:
                print(f"  note: loop ending failed, clip kept without it ({p3.stderr.strip()[-100:]})")
                plan["loop"] = False
        edit_tags = edits.tags(plan, bool(ecfg.get("remove_music")) and used != src)
    edit_tags += rf_tags
    save_preview(tmp, ass_path, chunks, cfg, row, day)
    shutil.move(str(tmp), str(mp4))  # Drive is a different disk: move, not rename
    txt.write_text(render.post_text(row), encoding="utf-8")
    print(f"  {row['batch_id']} {row['brand']}: {dur:.0f}s, {len(words)} words ({lang}) -> {mp4}")
    return {"fetch_status": "done", "render_status": "done", "output_path": str(mp4),
            "rendered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "template_version": str(tpl.get("version", "")) + ("+" + ",".join(edit_tags) if edit_tags else "")
                                + (f"|{clip_style.tag(style)}" if style else "")}


def main():
    settings = load_settings()
    cfg = dict(settings["render"])
    for key, env in (("output_dir", "CLIPPER_OUTPUT_DIR"), ("work_dir", "CLIPPER_WORK_DIR"),
                     ("fonts_dir", "CLIPPER_FONTS_DIR")):
        cfg[key] = os.environ.get(env, cfg[key])
    js_runtime = os.environ.get("YTDLP_JS_RUNTIME")
    try:
        ss = sheets.open_sheet(sheets.get_client(), settings["sheet"]["id"])
        sheets.ensure_tab(ss, TAB, TABS[TAB])
        ws = ss.worksheet(TAB)
        rows = sheets.read_records(ws)
    except sheets.SheetSetupError as e:
        print(f"RENDER FAILED (sheet): {e}")
        return 1
    limit = int(os.environ.get("CLIPPER_MAX_CLIPS") or 0) or cfg["max_clips_per_run"]
    max_age = cfg.get("max_batch_age_days", 1)
    todo = to_render(rows, limit, max_age)
    print(f"{len(todo)} approved clips to render (newest first; batches older than {max_age} day(s) skipped).")
    if not todo:
        print("Nothing to do. Type 'yes' in the approved column of the Batch tab first.")
        return 0
    transcriber = Transcriber(cfg["whisper_model"])
    playbook = read_playbook()
    style = prepare_style(cfg, playbook)
    ecfg = prepare_edits(cfg, Path(cfg["work_dir"]), playbook)
    day = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    headers = TABS[TAB]
    ok = 0
    for row in todo:
        try:
            result = render_one(row, cfg, transcriber, day, js_runtime, ecfg, style)
        except Exception as e:  # keep going with the other clips
            result = {"render_status": f"failed: {type(e).__name__}: {str(e)[:100]}"}
            if (Path(cfg["work_dir"]) / f"{row['batch_id']}.src.mp4").exists():
                result["fetch_status"] = "done"
        if result.get("blocked"):   # YouTube's bot check: nothing is marked failed, retried next run
            print("STOPPED: YouTube asks Colab to 'sign in to confirm you're not a bot'.\n"
                  "  Fix: Runtime > Disconnect and delete runtime, then Run all (new IP).\n"
                  "  If it keeps happening: put cookies.txt in Drive/GTA6 Clipper (see CLAUDE.md).")
            break
        sheets.set_values(ws, headers, "batch_id", {row["batch_id"]: result})
        if result.get("render_status") == "done":
            ok += 1
        else:
            print(f"  {row['batch_id']}: {result}")
    print(f"Rendered {ok}/{len(todo)}. Clips are in Google Drive -> {cfg['output_dir']}")
    return 0 if ok == len(todo) else 1


if __name__ == "__main__":
    sys.exit(main())
