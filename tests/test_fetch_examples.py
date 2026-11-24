from agents import fetch_examples as fe


def r(cat, rank, url, have="yes"):
    return {"category": cat, "rank": str(rank), "url": url, "have_skill": have}


def test_pick_order_missing_skills_first_then_top_unique():
    rows = [r(fe.TOP, 1, "u1"), r(fe.TOP, 2, "u2"), r("emoji / sticker pop-ups", 1, "u3", "no"),
            r("loop ending", 1, "u1", "no"), r("zoom punch-ins", 1, "u4"), r("emoji / sticker pop-ups", 2, "u5", "no"),
            r("screen shake", 1, "")]
    urls = [x["url"] for x in fe.pick(rows, 10)]
    assert urls == ["u3", "u1", "u2", "u5", "u4"]
    assert len(fe.pick(rows, 2)) == 2


def test_video_id_and_slug():
    assert fe.video_id("https://youtube.com/shorts/_LVLtceZy3I") == "_LVLtceZy3I"
    assert fe.video_id("https://www.youtube.com/watch?v=Dz0naoD1QWo&t=5") == "Dz0naoD1QWo"
    assert fe.slug("emoji / sticker pop-ups") == "emoji-sticker-pop-ups"
