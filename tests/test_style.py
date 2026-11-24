from lib import render, style

DEF = {"title_label": False, "hook_text": True, "hook_kind": "bold_claim", "hook_words": 5,
       "captions": "phrase", "caption_position": "middle", "caption_keyword_color": False, "layout": "blur"}


def feat(**kw):
    f = {"has_title_label": False, "hook_type": "action_first", "caption_style": "none",
         "caption_position": "none", "caption_keyword_color": False, "layout": "blurred_background",
         "spoken_language": "en"}
    f.update(kw)
    return f


def test_not_enough_winners_uses_defaults():
    st, why, ex = style.choose([feat()] * 3, [feat()] * 3, DEF)
    assert st == DEF and "default" in why["captions"] and ex == []


def test_elements_follow_winners():
    win = [feat(has_title_label=True, caption_style="word_by_word", caption_position="middle",
                caption_keyword_color=True, layout="full_vertical",
                hook_text_kind="question", hook_text_on_screen="Did he really survive that?")] * 7 + \
          [feat(hook_text_kind="none", hook_text_on_screen="")] * 3
    avg = [feat(hook_text_kind="none")] * 10
    st, why, ex = style.choose(win, avg, DEF)
    assert st["title_label"] is True and "70% of winners" in why["title_label"]
    assert st["hook_text"] is True and st["hook_kind"] == "question" and st["hook_words"] == 5
    assert st["captions"] == "word_by_word" and st["caption_position"] == "middle"
    assert st["caption_keyword_color"] is True and st["layout"] == "fill"
    assert ex[0] == "Did he really survive that?"


def test_captions_judged_on_winners_with_speech_only():
    win = [feat(spoken_language="none")] * 8 + [feat(caption_style="phrase_by_phrase",
                                                       caption_position="bottom")] * 4
    st, why, _ = style.choose(win, [feat()] * 10, DEF)
    assert st["captions"] == "phrase" and st["caption_position"] == "bottom"   # 4/4 with speech
    st, _, _ = style.choose([feat()] * 10, [feat()] * 10, DEF)
    assert st["captions"] == "none" and st["title_label"] is False and st["hook_text"] is False


def test_playbook_round_trip_and_tag():
    st, why, ex = style.choose([feat(has_title_label=True)] * 10, [feat()] * 10, DEF)
    rows = style.playbook_rows(st, why, ex, "src", "now")
    back, why2, ex2 = style.from_playbook(rows, DEF)
    assert back == st and why2["title_label"].startswith("title_label: on")
    assert style.tag(back) == "none,nohook,label,blur"


TPL = render.load_template("wasted_again")
WORDS = [{"word": w, "start": i * 0.4, "end": i * 0.4 + 0.35}
         for i, w in enumerate("he flipped the cop car".split())]


def test_build_ass_hides_what_winners_dont_use():
    chunks = render.chunk_words(WORDS, 3, 0.45)
    ass = render.build_ass(TPL, chunks, "COP FLIP", "BRO WHAT", "@x", 8,
                           style={"title_label": False, "hook_text": False, "captions": "none"})
    assert "COP FLIP" not in ass and "BRO WHAT" not in ass and "Caption,," not in ass and "via @x" in ass


def test_word_by_word_and_pop():
    chunks = render.chunk_words(WORDS, 3, 0.45)
    ass = render.build_ass(TPL, chunks, "", "", "", 8, style={"captions": "word_by_word",
                                                              "caption_position": "middle"})
    lines = [l for l in ass.splitlines() if ",Caption,," in l]
    assert len(lines) == 5 and all("\\t(0,90" in l for l in lines) and "WrapStyle: 2" in ass


def test_fit_size_and_fill_layout():
    assert render.fit_size("SHORT", 104) == 104
    assert render.fit_size("A VERY LONG CAPTION LINE HERE", 104) < 104
    assert render.layout_boxes(1920, 1080, {"layout": "fill"}) == (1080, 1920, 0)
