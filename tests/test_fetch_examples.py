from agents import fetch_examples as fe


def r(cat, rank, url, trick=""):
    return {"category": cat, "rank": str(rank), "url": url, "trick_rank": str(trick)}


def test_pick_trick1_first_then_trick2_then_top_unique():
    rows = [r(fe.TOP, 1, "u1"), r("screen shake", 2, "u3", 1), r("screen shake", 1, "u2", 1),
            r("emoji / sticker pop-ups", 1, "u2", 2), r("emoji / sticker pop-ups", 2, "u4", 2), r("x", 1, "", 3)]
    assert [x["url"] for x in fe.pick(rows, 10)] == ["u2", "u3", "u4", "u1"]
    assert len(fe.pick(rows, 2)) == 2


def test_video_id_and_slug():
    assert fe.video_id("https://youtube.com/shorts/_LVLtceZy3I") == "_LVLtceZy3I"
    assert fe.video_id("https://www.youtube.com/watch?v=Dz0naoD1QWo&t=5") == "Dz0naoD1QWo"
    assert fe.slug("emoji / sticker pop-ups") == "emoji-sticker-pop-ups"


def test_tricks_we_cannot_do_yet_come_first():
    rows = [r("loop ending", 1, "a1", 1), r("loop ending", 2, "a2", 1), r("cuts on the beat", 1, "b1", 4),
            r("cuts on the beat", 2, "b2", 4), r(fe.TOP, 1, "t1")]
    rows[0]["have_skill"] = rows[1]["have_skill"] = "yes"
    rows[2]["have_skill"] = rows[3]["have_skill"] = "no"
    assert [x["url"] for x in fe.pick(rows, 10)] == ["b1", "b2", "a1", "a2", "t1"]
