from types import SimpleNamespace

from agents import render_batch
from lib import render

TPL = render.load_template("wasted_again")


def w(word, start, end):
    return {"word": word, "start": start, "end": end}


def test_templates_exist_for_every_brand():
    from lib.config import load_settings
    for b in load_settings()["brands"]:
        t = render.load_template(b["key"])
        assert t["brand"] == b["key"] and len(t["accent"]) == 6 and t["version"] >= 1


def test_chunk_words_splits_on_count_pause_and_punctuation():
    words = [w("no", 0, .2), w("way", .25, .5), w("bro.", .55, .8), w("look", .9, 1.1),
             w("at", 1.15, 1.2), w("this", 1.25, 1.5), w("car", 1.55, 1.8), w("wow", 3.0, 3.3)]
    chunks = render.chunk_words(words, max_words=3, pause_split=0.45)
    assert [c["words"] for c in chunks] == [["no", "way", "bro."], ["look", "at", "this"], ["car"], ["wow"]]
    assert chunks[2]["end"] <= 1.8 + 0.6 + 1e-9          # no hanging during the pause
    assert chunks[-1]["end"] == 3.3 + 0.3


def test_keyword_index():
    assert render.keyword_index(["the", "cops", "flipped"]) == 2
    assert render.keyword_index(["200", "feet"]) == 0
    assert render.keyword_index(["no", "way"]) is None


def test_ass_color_and_time():
    assert render.ass_color("FF3B30") == "&H00303BFF"
    assert render.ass_color("FFFFFF", 0.25) == "&H40FFFFFF"
    assert render.ass_time(3723.456) == "1:02:03.46"


def test_build_ass_has_label_hook_credit_and_highlight():
    chunks = render.chunk_words([w("the", 0, .3), w("cops", .35, .6), w("flipped", .65, 1.0)])
    ass = render.build_ass(TPL, chunks, "Cop car flip", "GTA 6 cops are insane", "@Creator", 10.0)
    assert "COP CAR FLIP" in ass and "GTA 6 COPS ARE INSANE" in ass and "via @Creator" in ass
    assert render.ass_color(TPL["accent"]) + "}FLIPPED" in ass
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    # captions stay above the bottom 20% safe zone
    import re
    ys = [int(m) for m in re.findall(r"\\pos\(\d+,(\d+)\)", ass)]
    assert max(ys) < render.SAFE_BOTTOM


def test_ass_escapes_braces():
    ass = render.build_ass(TPL, [], "a {b}", "", "", 5.0)
    assert "A (B)" in ass


def test_layout_horizontal_and_vertical():
    fw, fh, top = render.layout_boxes(1920, 1080, TPL)
    assert fw == 1080 and top == (1920 - fh) // 2 and fh == round(1350 * 1080 / 1920 / 2) * 2
    assert render.layout_boxes(1080, 1920, TPL) == (1080, 1920, 0)


def test_ffmpeg_cmd():
    cmd = render.ffmpeg_cmd("in.mp4", "/c/s.ass", "/c/fonts", "out.mp4", TPL, 1920, 1080, 25.0)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "boxblur" in fc and "overlay=0:" in fc and "ass='/c/s.ass'" in fc
    assert "loudnorm=I=-14" in " ".join(cmd) and cmd[-1] == "out.mp4" and "veryfast" in cmd
    silent = render.ffmpeg_cmd("in.mp4", "s.ass", "f", "o.mp4", TPL, 1920, 1080, 25.0, has_audio=False)
    assert "-af" not in silent


def test_to_render_selects_approved_unrendered():
    rows = [{"batch_id": "1", "approved": "yes"}, {"batch_id": "2", "approved": "YES ", "render_status": "done"},
            {"batch_id": "3", "approved": ""}, {"batch_id": "4", "approved": "✅", "render_status": "failed: x"}]
    assert [r["batch_id"] for r in render_batch.to_render(rows, 10)] == ["1", "4"]
    assert len(render_batch.to_render(rows, 1)) == 1


def test_ytdlp_cmd_downloads_only_the_section():
    cmd = render_batch.ytdlp_cmd({"video_id": "abc", "start_s": "105", "end_s": "130"}, "/w/x.mp4", "node")
    assert "*105-130" in cmd and "--force-keyframes-at-cuts" in cmd and cmd[-1].endswith("v=abc")
    assert cmd[cmd.index("--js-runtimes") + 1] == "node"


def test_output_paths_and_post_text():
    row = {"batch_id": "2026-11-20-01", "brand": "neon_wanted", "title": "T", "description": "D",
           "hashtags": "#GTA6", "video_id": "v", "start_s": "5"}
    mp4, txt = render_batch.output_paths("/d", row, "2026-11-20")
    assert str(mp4) == "/d/2026-11-20/neon_wanted/2026-11-20-01.mp4" and txt.suffix == ".txt"
    text = render.post_text(row)
    assert "TITLE:\nT" in text and "https://youtu.be/v?t=5" in text


def test_words_from_segments():
    seg = SimpleNamespace(words=[SimpleNamespace(word=" hi", start=0.1, end=0.3),
                                 SimpleNamespace(word=" ", start=0.3, end=0.31)])
    assert render_batch.words_from_segments([seg, SimpleNamespace(words=None)]) == [{"word": "hi", "start": 0.1, "end": 0.3}]


def test_load_audio_is_16k_mono_float(tmp_path):
    import shutil
    import subprocess

    import pytest
    pytest.importorskip("numpy")
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed here (Colab has it)")
    src = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-ac", "2", str(src)], check=True)
    a = render_batch.load_audio(src)
    assert a.dtype.name == "float32" and abs(len(a) - 32000) < 400 and 0 < abs(a).max() <= 1.0


def test_hook_is_above_the_label_and_off_the_gameplay():
    import re
    ass = render.build_ass(TPL, [], "Cop car flip", "Cops are insane", "", 10.0)
    hook_line = next(l for l in ass.splitlines() if "COPS ARE INSANE" in l)
    hook_y = int(re.search(r"\\pos\(\d+,(\d+)\)", hook_line).group(1))
    _, _, top = render.layout_boxes(1920, 1080, TPL)
    assert hook_y < TPL["label"]["y"] < top



def test_looping_clips_keep_the_hook_on_screen_without_pop():
    short = next(l for l in render.build_ass(TPL, [], "", "Cops are insane", "", 10.0).splitlines()
                 if "COPS ARE INSANE" in l)
    full = next(l for l in render.build_ass(TPL, [], "", "Cops are insane", "", 10.0, hook_full=True).splitlines()
                if "COPS ARE INSANE" in l)
    assert "0:00:10.00" not in short and "\\fad" in short        # normal: pops in, gone after a few s
    assert ",0:00:00.00,0:00:10.00," in full and "\\fad" not in full and "\\t(" not in full

def test_mask_words():
    words = [{"word": "what", "start": 0, "end": 1}, {"word": "Fucking", "start": 1, "end": 2},
             {"word": "shit!", "start": 2, "end": 3}, {"word": "dickens", "start": 3, "end": 4},
             {"word": "chutiya", "start": 4, "end": 5}]
    out = [w["word"] for w in render.mask_words(words, ["fuck*", "shit", "dick", "chutiya"])]
    assert out == ["what", "F******", "s***", "dickens", "c******"]
    assert render.mask_words(words, [])[1]["word"] == "Fucking"


# --- GPU -> CPU fallback (Colab: "libcublas.so.12 is not found") ---------------------------------

def make_transcriber(monkeypatch, models):
    """Transcriber whose _load returns scripted fake models; first device is cuda."""
    import numpy as np
    loads = []

    def fake_load(self, device):
        loads.append(device)
        return models[device]
    monkeypatch.setattr(render_batch.Transcriber, "_load", fake_load)
    monkeypatch.setattr(render_batch, "preload_cuda_libs", lambda: [])
    monkeypatch.setattr(render_batch, "load_audio", lambda path: np.ones(16000, dtype="float32"))
    import sys, types
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: True)))
    return render_batch.Transcriber("small"), loads


class FakeModel:
    def __init__(self, error=None):
        self.error, self.calls = error, 0

    def transcribe(self, audio, **kw):
        self.calls += 1
        if self.error:
            def gen():
                raise self.error
                yield
            return gen(), None      # like faster-whisper: the error appears when segments are read
        word = type("W", (), {"word": " hi", "start": 0.0, "end": 0.5})()
        seg = type("S", (), {"words": [word]})()
        return iter([seg]), type("I", (), {"language": "en"})()


def test_gpu_failure_falls_back_to_cpu_once(monkeypatch):
    gpu = FakeModel(RuntimeError("Library libcublas.so.12 is not found or cannot be loaded"))
    cpu = FakeModel()
    t, loads = make_transcriber(monkeypatch, {"cuda": gpu, "cpu": cpu})
    words, lang = t.words("x.mp4")
    assert words == [{"word": "hi", "start": 0.0, "end": 0.5}] and lang == "en"
    assert loads == ["cuda", "cpu"] and t.device == "cpu"
    t.words("y.mp4")                       # later clips go straight to the CPU model
    assert gpu.calls == 1 and cpu.calls == 2 and loads == ["cuda", "cpu"]


def test_other_errors_are_not_swallowed(monkeypatch):
    t, loads = make_transcriber(monkeypatch, {"cuda": FakeModel(ValueError("bad audio")), "cpu": FakeModel()})
    import pytest
    with pytest.raises(ValueError):
        t.words("x.mp4")
    assert loads == ["cuda"]


def test_is_gpu_error():
    assert render_batch.is_gpu_error(RuntimeError("Library libcudnn_ops.so.9 not found"))
    assert not render_batch.is_gpu_error(ValueError("bad audio"))


def test_preload_cuda_libs_never_raises():
    assert isinstance(render_batch.preload_cuda_libs(), list)


def test_cookies_and_bot_block(tmp_path, monkeypatch):
    from agents import render_batch as rb
    row = {"start_s": "1", "end_s": "9", "video_id": "abc"}
    monkeypatch.delenv("YTDLP_COOKIES", raising=False)
    assert "--cookies" not in rb.ytdlp_cmd(row, "o.mp4")
    c = tmp_path / "cookies.txt"; c.write_text("x")
    monkeypatch.setenv("YTDLP_COOKIES", str(c))
    cmd = rb.ytdlp_cmd(row, "o.mp4")
    assert cmd[cmd.index("--cookies") + 1] == str(c)
    assert rb.is_bot_block("ERROR: [youtube] x: Sign in to confirm you’re not a bot.")
    assert not rb.is_bot_block("ERROR: ffmpeg exited with code 222")
