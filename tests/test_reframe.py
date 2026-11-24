import numpy as np

from lib import reframe, render


def moving_blob(n=12, w=64, h=36, start=10, step=3):
    frames = np.zeros((n, h, w), np.float32)
    for i in range(n):
        x = start + i * step
        frames[i, 10:20, x:x + 6] = 255
    return frames


def test_motion_follows_the_moving_thing_and_holds_when_still():
    pts = reframe.motion_points(moving_blob(), fps=4)
    xs = [cx for _, cx in pts]
    assert xs[0] < xs[-1] and 0.15 < xs[0] < 0.4 and xs[-1] > 0.6
    still = reframe.motion_points(np.zeros((5, 36, 64), np.float32), fps=4)
    assert all(cx is None for _, cx in still)


def test_facecam_area_is_ignored_for_motion():
    f = moving_blob()
    f[:, 0:10, 54:64] = np.random.default_rng(0).integers(0, 255, (12, 10, 10))   # busy webcam corner
    pts = reframe.motion_points(f, fps=4, exclude=(54 / 64, 0, 10 / 64, 10 / 36))
    assert max(cx for _, cx in pts) < 0.85


def test_smooth_path_limits_speed():
    pts = [(i * 0.25, 0.1 if i < 4 else 0.9) for i in range(12)]
    path = reframe.smooth_path(pts, max_speed=0.3, window=1)
    steps = [abs(b[1] - a[1]) for a, b in zip(path, path[1:])]
    assert max(steps) <= 0.3 * 0.25 + 1e-9 and path[-1][1] > path[3][1]


def test_crop_expression_is_flat_and_clamped():
    path = [(0.0, 0.0), (1.0, 0.5), (2.0, 1.0)]
    e = reframe.crop_x_expr(path, 3832, 1080)
    assert e.startswith("lt(t,0.0)*0") and "gte(t,2.0)*2752" in e and "if(" not in e
    assert reframe.crop_x_expr([(0, 0.5), (1, 0.5)], 3832, 1080) == "1376"
    assert reframe.crop_x_expr(path, 1000, 1080) is None


def test_stable_face_needs_same_corner_spot():
    faces = [(i, 540, 40, 30, 30) for i in range(5)] + [(2, 300, 150, 30, 30)]
    assert reframe.stable_face(faces, 8, 640, 320) == (540, 40, 30, 30)
    middle = [(i, 305, 145, 30, 30) for i in range(6)]
    assert reframe.stable_face(middle, 8, 640, 320) is None      # a face mid-screen is gameplay
    assert reframe.stable_face(faces[:2], 8, 640, 320) is None    # not in most frames


def test_webcam_box_from_overlay_edges():
    avg = np.full((320, 640), 60, np.float32)
    avg[30:110, 500:636] = 180                                    # sharp overlay rectangle
    x, y, w, h = reframe.webcam_box(avg, (547, 50, 36, 36))
    assert abs(x - 499) <= 2 and abs(y - 29) <= 2 and abs(w - 137) <= 3 and abs(h - 81) <= 3


def test_split_layout_boxes_and_cmd():
    ch = render.split_cam_h((0.79, 0.09, 0.2, 0.24), 1920, 1080)
    assert 480 <= ch <= 806 and ch % 2 == 0
    tpl = render.load_template("wasted_again")
    tpl.update(layout="split", split_cam_h=ch)
    assert render.layout_boxes(1920, 1080, tpl) == (1080, 1920 - ch, ch)
    rf = {"facecam": (0.79, 0.09, 0.2, 0.24), "path": [(0.0, 0.2), (2.0, 0.8)]}
    cmd = " ".join(render.ffmpeg_cmd("s.mp4", "a.ass", "f", "o.mp4", tpl, 1920, 1080, 10, True, None, rf))
    assert "vstack=inputs=2" in cmd and "crop=iw*0.2000:ih*0.2400" in cmd and "gte(t," in cmd
    tpl["layout"] = "fill"
    cmd = " ".join(render.ffmpeg_cmd("s.mp4", "a.ass", "f", "o.mp4", tpl, 1920, 1080, 10, True, None, rf))
    assert "scale=-2:1920,crop=1080:1920:x='" in cmd and "vstack" not in cmd


def test_split_crop_keeps_the_facecam_out_of_the_gameplay():
    path = [(0.0, 0.95), (1.0, 0.95)]                   # action pulls right, where the cam is
    e = reframe.crop_x_expr(path, 2558, 1080, avoid=(0.79, 0.2))
    assert int(e) <= round(0.79 * 2558) - 1080
