from agents import publish_batch
from lib.config import load_rules, load_settings

S, R = load_settings(), load_rules()
DISC = R["packaging"]["disclaimer"]


def clip(**kw):
    c = {"moment_id": "v1-g105", "brand": "wasted_again", "content_type": "chaos",
         "hook_text": "GTA 6 cops are insane", "title_label": "COP CAR FLIP",
         "title": "GTA 6 police chase goes wrong 😂",
         "description": f"Cop car flips in GTA 6\n🎥 Clip from @creator\n{DISC}",
         "hashtags": "#GTA6 #GTA6Shorts #Shorts #police", "creator_credit": "@creator"}
    c.update(kw)
    return c


MOMENTS = {"v1-g105": {"moment_id": "v1-g105", "video_id": "v1", "start_s": "105", "end_s": "130",
                       "gate_status": "ready"},
           "v2-g5": {"moment_id": "v2-g5", "video_id": "v2", "start_s": "5", "end_s": "30",
                     "gate_status": "rejected_low"}}


def test_valid_clip_becomes_row():
    good, bad = publish_batch.build_rows({"batch_id": "2026-11-20", "clips": [clip()]}, MOMENTS, [], S, R)
    assert bad == []
    row = good[0]
    assert row["batch_id"] == "2026-11-20-01" and row["video_id"] == "v1"
    assert row["start_s"] == 105 and row["end_s"] == 130 and row["approved"] == ""


def test_problems_are_reported():
    bad_clip = clip(brand="nope", title="x" * 70, hashtags="#GTA6", hook_text="one two three four five six seven",
                    title_label="ONE", description="no credit", content_type="drama")
    good, bad = publish_batch.build_rows({"batch_id": "d", "clips": [bad_clip]}, MOMENTS, [], S, R)
    assert good == []
    msg = " | ".join(bad[0][1])
    for part in ["unknown brand", "content_type", "hook_text", "title_label", "title missing or longer",
                 "hashtags missing", "disclaimer", "creator_credit"]:
        assert part in msg


def test_moment_must_be_ready_and_unused():
    good, bad = publish_batch.build_rows({"batch_id": "d", "clips": [clip(moment_id="v2-g5"), clip(moment_id="nope")]},
                                         MOMENTS, [], S, R)
    assert len(bad) == 2 and "not ready" in bad[0][1][0] and "not found" in bad[1][1][0]
    existing = [{"batch_id": "2026-11-19-03", "moment_id": "v1-g105", "brand": "neon_wanted"}]
    good, bad = publish_batch.build_rows({"batch_id": "2026-11-20", "clips": [clip()]}, MOMENTS, existing, S, R)
    assert "already in Batch" in bad[0][1][0]
    # re-publishing the same batch file is fine (same clip id)
    existing = [{"batch_id": "2026-11-20-01", "moment_id": "v1-g105", "brand": "wasted_again"}]
    good, bad = publish_batch.build_rows({"batch_id": "2026-11-20", "clips": [clip()]}, MOMENTS, existing, S, R)
    assert bad == [] and len(good) == 1
