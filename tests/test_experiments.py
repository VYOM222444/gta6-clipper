from lib import experiments as ex


def post(bid, **views):
    return dict({"batch_id": bid, "platform": "youtube"}, **{k: str(v) for k, v in views.items()})


def test_tags():
    assert ex.tags_of("2+zoom2,impact,shake,split|phrase-middle,hook") >= {"shake", "split", "zoom2", "hook"}


def test_report_uses_earliest_solid_checkpoint_and_medians():
    batch = [{"batch_id": f"b{i}", "template_version": "2+impact,shake" if i < 4 else "2+impact"}
             for i in range(8)]
    posted = [post(f"b{i}", views_6h=v) for i, v in enumerate([400, 300, 500, 90000, 100, 120, 80, 150])]
    res = ex.report(posted, batch, ["shake", "freeze"])
    assert len(res) == 1                                  # freeze: nobody has it yet
    c = res[0]
    assert c["checkpoint"] == "views_6h" and c["n_on"] == 4 and c["n_off"] == 4
    assert c["median_on"] == 450 and c["median_off"] == 110     # the 90k outlier doesn't decide
    assert c["verdict"].startswith("LOOKS BETTER") and "early" in c["verdict"]
    rows = ex.playbook_rows(res, "now")
    assert rows[0]["rule_id"] == "experiments.shake" and "at 6h with it (4 posts)" in rows[0]["rule"]


def test_24h_preferred_when_enough_and_instagram_ignored():
    batch = [{"batch_id": f"b{i}", "template_version": "freeze" if i % 2 else ""} for i in range(6)]
    posted = [post(f"b{i}", views_6h=10, views_24h=100 + i) for i in range(6)] + \
             [{"batch_id": "b1", "platform": "instagram", "views_24h": "999999"}]
    c = ex.report(posted, batch, ["freeze"])[0]
    assert c["checkpoint"] == "views_24h" and c["verdict"] == "no clear difference (early, few posts)"
