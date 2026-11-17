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
from datetime import datetime, timezone
from pathlib import Path

from lib import render, sheets
from lib.config import load_rules, load_settings
from lib.schema import TABS

TAB = "Batch"
YES = {"yes", "y", "✅", "ok", "true"}
FORMAT = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b[height<=1080]/b"


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def to_render(rows, limit):
    out = []
    for r in rows:
        if str(r.get("approved", "")).strip().lower() not in YES:
            continue
        if r.get("render_status") == "done" or not r.get("batch_id"):
            continue
        out.append(r)
    return out[:limit]


def ytdlp_cmd(row, out_path, js_runtime=None):
    start, end = int(_f(row["start_s"])), int(_f(row["end_s"]))
    cmd = [sys.executable, "-m", "yt_dlp", "-f", FORMAT, "--download-sections", f"*{start}-{end}",
           "--force-keyframes-at-cuts", "--merge-output-format", "mp4", "--no-playlist",
           "--no-warnings", "--no-progress", "-o", str(out_path)]
    if js_runtime:
        cmd += ["--js-runtimes", js_runtime]
    cmd.append(f"https://www.youtube.com/watch?v={row['video_id']}")
    return cmd


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
    return int(v.get("width", 1920)), int(v.get("height", 1080)), float(d["format"]["duration"]), has_audio


class Transcriber:
    def __init__(self, size):
        from faster_whisper import WhisperModel
        try:
            import torch
            cuda = torch.cuda.is_available()
        except ImportError:
            cuda = False
        self.model = WhisperModel(size, device="cuda" if cuda else "cpu",
                                  compute_type="float16" if cuda else "int8")
        print(f"Whisper '{size}' on {'GPU' if cuda else 'CPU'}")

    def words(self, path):
        # Decode with ffmpeg ourselves: faster-whisper's own decoder (PyAV) breaks when Colab
        # ships a newer PyAV ("open() got an unexpected keyword argument 'metadata_errors'").
        audio = load_audio(path)
        if audio.size == 0:
            return [], ""
        segments, info = self.model.transcribe(audio, word_timestamps=True, vad_filter=True)
        return words_from_segments(list(segments)), info.language


def load_audio(path, sr=16000):
    """Audio as mono float32 numpy array at 16 kHz (what Whisper expects), via ffmpeg."""
    import numpy as np
    p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
                        "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(p.stdout, np.int16).astype(np.float32) / 32768.0


def render_one(row, cfg, transcriber, day, js_runtime):
    work = Path(cfg["work_dir"])
    work.mkdir(parents=True, exist_ok=True)
    src = work / f"{row['batch_id']}.src.mp4"
    if not src.exists():
        p = subprocess.run(ytdlp_cmd(row, src, js_runtime), capture_output=True, text=True)
        if p.returncode != 0 or not src.exists():
            msg = ((p.stderr or p.stdout).strip().splitlines() or ["download failed"])[-1]
            return {"fetch_status": f"failed: {msg[:120]}"}
    w, h, dur, has_audio = probe(src)
    words, lang = transcriber.words(src) if has_audio else ([], "")
    words = render.mask_words(words, load_rules().get("captions", {}).get("mask_words", []))
    tpl = render.load_template(row["brand"])
    chunks = render.chunk_words(words, tpl["caption"]["max_words"], tpl["caption"]["pause_split"])
    ass_path = work / f"{row['batch_id']}.ass"
    ass_path.write_text(render.build_ass(tpl, chunks, row.get("title_label", ""), row.get("hook_text", ""),
                                         row.get("creator_credit", ""), dur, w, h), encoding="utf-8")
    mp4, txt = output_paths(cfg["output_dir"], row, day)
    mp4.parent.mkdir(parents=True, exist_ok=True)
    tmp = work / f"{row['batch_id']}.out.mp4"
    p = subprocess.run(render.ffmpeg_cmd(src, ass_path, cfg["fonts_dir"], tmp, tpl, w, h, dur, has_audio),
                       capture_output=True, text=True)
    if p.returncode != 0:
        return {"fetch_status": "done", "render_status": f"failed: {p.stderr.strip()[-120:]}"}
    shutil.move(str(tmp), str(mp4))  # Drive is a different disk: move, not rename
    txt.write_text(render.post_text(row), encoding="utf-8")
    print(f"  {row['batch_id']} {row['brand']}: {dur:.0f}s, {len(words)} words ({lang}) -> {mp4}")
    return {"fetch_status": "done", "render_status": "done", "output_path": str(mp4),
            "rendered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "template_version": str(tpl.get("version", ""))}


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
    todo = to_render(rows, cfg["max_clips_per_run"])
    print(f"{len(todo)} approved clips to render.")
    if not todo:
        print("Nothing to do. Type 'yes' in the approved column of the Batch tab first.")
        return 0
    transcriber = Transcriber(cfg["whisper_model"])
    day = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    headers = TABS[TAB]
    ok = 0
    for row in todo:
        try:
            result = render_one(row, cfg, transcriber, day, js_runtime)
        except Exception as e:  # keep going with the other clips
            result = {"render_status": f"failed: {type(e).__name__}: {str(e)[:100]}"}
            if (Path(cfg["work_dir"]) / f"{row['batch_id']}.src.mp4").exists():
                result["fetch_status"] = "done"
        sheets.set_values(ws, headers, "batch_id", {row["batch_id"]: result})
        if result.get("render_status") == "done":
            ok += 1
        else:
            print(f"  {row['batch_id']}: {result}")
    print(f"Rendered {ok}/{len(todo)}. Clips are in Google Drive -> {cfg['output_dir']}")
    return 0 if ok == len(todo) else 1


if __name__ == "__main__":
    sys.exit(main())
