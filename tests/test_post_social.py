from datetime import datetime, timedelta, timezone

import pytest

from agents import post_social as ps

NOW = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)
TOK = {"wasted_again": {"ig_user_id": "IG1", "page_id": "PG1", "token": "SECRET"},
       "neon_wanted": {"ig_user_id": "IG2", "token": "SECRET2"}}
CFG = {"veto_hours": 2, "max_age_hours": 48, "max_per_brand_per_run": 2}


def row(bid, brand="wasted_again", hours_ago=3, **kw):
    t = NOW - timedelta(hours=hours_ago)
    return {"batch_id": bid, "brand": brand, "render_status": "done", "approved": "yes",
            "rendered_at": ps.iso(t), "ig_caption": "cap #GTA6", **kw}


def test_due_respects_veto_approval_caps_and_done():
    rows = [row("a"), row("b"), row("c"),                    # cap 2 for wasted_again
            row("d", hours_ago=1),                           # inside the veto window
            row("e", approved="no"),                         # owner vetoed
            row("f", brand="neon_wanted"),                   # no page_id -> instagram only
            row("g", brand="busted_again"),                  # no token
            row("h", brand="neon_wanted", ig_status="done"),  # already posted
            row("i", brand="neon_wanted", hours_ago=99)]     # too old
    got = [(r["batch_id"], p) for r, p in ps.due(rows, TOK, CFG, NOW)]
    assert got == [("a", "instagram"), ("a", "facebook"), ("b", "instagram"), ("b", "facebook"),
                   ("f", "instagram")]


def test_load_tokens():
    assert ps.load_tokens({}) == {}
    assert ps.load_tokens({ps.ENV: '{"x": {"token": "t"}, "y": {}}'}) == {"x": {"token": "t"}}
    with pytest.raises(ps.MetaError):
        ps.load_tokens({ps.ENV: "not json"})


class Resp:
    def __init__(self, data, code=200):
        self.data, self.status_code, self.text = data, code, str(data)

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, statuses=("IN_PROGRESS", "FINISHED")):
        self.calls, self.statuses = [], list(statuses)

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, headers))
        if url.endswith("/IG1/media"):
            return Resp({"id": "C1"})
        if "rupload" in url:
            assert headers["file_size"] == "4" and headers["Authorization"] == "OAuth SECRET"
            return Resp({"success": True})
        if url.endswith("media_publish"):
            return Resp({"id": "M1"})
        if url.endswith("video_reels"):
            return Resp({"video_id": "V1"} if data["upload_phase"] == "start" else {"success": True})
        return Resp({"error": {"message": "unexpected"}}, 400)

    def get(self, url, params=None, timeout=None):
        if url.endswith("/C1"):
            return Resp({"status_code": self.statuses.pop(0)})
        if url.endswith("/M1"):
            return Resp({"permalink": "https://www.instagram.com/reel/abc/"})
        return Resp({}, 404)


def test_instagram_flow():
    m = ps.Meta("SECRET", session=FakeSession(), sleep=lambda s: None)
    assert m.instagram("IG1", b"abcd", "cap") == ("M1", "https://www.instagram.com/reel/abc/")


def test_instagram_processing_error():
    m = ps.Meta("SECRET", session=FakeSession(statuses=["ERROR"]), sleep=lambda s: None)
    with pytest.raises(ps.MetaError):
        m.instagram("IG1", b"abcd", "cap")


def test_facebook_flow():
    m = ps.Meta("SECRET", session=FakeSession(), sleep=lambda s: None)
    assert m.facebook("PG1", b"abcd", "cap") == ("V1", "https://www.facebook.com/reel/V1")
