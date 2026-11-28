"""Examples tab: which editing tricks are going viral RIGHT NOW, with 10 real examples of each.

Tricks are ranked by how much more the Shorts winning right now use them than average Shorts
(last 72 h rising vs average when there is enough fresh data, else 7-day winners + rising vs
average). The top `tricks` get `per` example Shorts each (best breakouts first), plus a
"TOP breakouts" list of any style. The owner downloads them (Colab, agents/fetch_examples.py) and
sends them to Claude in chat to build the skill. Fixed slots, overwritten every Researcher run.
"""
import json

from lib.skills import SKILLS

TOP = "TOP breakouts (any style)"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def describe(f):
    """Short human summary of a Short's editing from its Gemini tags."""
    bits = []
    if f.get("format"):
        bits.append(str(f["format"]).replace("_", " "))
    if f.get("layout"):
        bits.append(str(f["layout"]).replace("_", " "))
    if f.get("caption_style") not in (None, "", "none"):
        bits.append(f"captions {str(f['caption_style']).replace('_', ' ')} {f.get('caption_position', '')}".strip())
    if f.get("hook_text_on_screen"):
        bits.append(f"hook \"{f['hook_text_on_screen']}\"")
    for key, name in (("zoom_ins", "zooms"), ("screen_shake", "shake"), ("freeze_frame", "freeze"),
                      ("slow_motion", "slow-mo"), ("emoji_or_stickers", "emoji"), ("loop_ending", "loop")):
        v = f.get(key)
        if v and v is not False:
            bits.append(name)
    sfx = [s for s in (f.get("sound_effects") or []) if s and s != "none"]
    if sfx:
        bits.append("sfx " + "/".join(sfx))
    if f.get("cuts"):
        bits.append(f"{f['cuts']} cuts")
    if f.get("story"):
        bits.append(f"story: {f['story']}")
    return ", ".join(bits)[:400]


def _share(feats, test):
    return sum(1 for f in feats if test(f)) / len(feats) if feats else 0.0


def viral_tricks(win, avg, min_use=0.15):
    """[{key, name, have, w, a, lift}] for every trick, most 'viral right now' first:
    used by >= min_use of winners and more than by average Shorts."""
    out = []
    for key, (name, test, have) in SKILLS.items():
        w, a = _share(win, test), _share(avg, test)
        out.append({"key": key, "name": name, "have": have, "w": w, "a": a, "lift": w - a})
    hot = [t for t in out if t["w"] >= min_use and t["lift"] > 0]
    return sorted(hot, key=lambda t: -t["lift"])


def build(entries, tricks_ranked, updated_at, tricks=5, per=10, top=10):
    """entries: [(row, features)] of winners / rising Shorts. -> Examples tab records."""
    ranked = sorted(entries, key=lambda e: (_num(e[0].get("breakout")), _num(e[0].get("views"))), reverse=True)

    def rec(slot, cat, trick_rank, rank, t, item):
        r, f = item if item else ({}, {})
        return {"example_id": f"{slot}#{rank}", "category": cat, "rank": rank,
                "have_skill": "" if item is None or t is None else ("yes" if t["have"] else "no"),
                "url": r.get("url", ""), "title": r.get("title", ""), "channel_title": r.get("channel_title", ""),
                "views": r.get("views", ""), "breakout": r.get("breakout", ""),
                "age_days": r.get("age_days", ""), "style": describe(f) if item else "",
                "updated_at": updated_at, "trick_rank": trick_rank,
                "trick_use": "" if t is None else f"winners {round(t['w'] * 100)}% vs average {round(t['a'] * 100)}%"}

    out = []
    for k in range(tricks):
        t = tricks_ranked[k] if k < len(tricks_ranked) else None
        hits = [e for e in ranked if t and SKILLS[t["key"]][1](e[1])][:per]
        for i in range(per):
            out.append(rec(f"trick{k + 1}", t["name"] if t else "", k + 1, i + 1, t,
                           hits[i] if i < len(hits) else None))
    for i in range(top):
        out.append(rec("top", TOP, "", i + 1, None, ranked[i] if i < len(ranked) else None))
    return out


def playbook_rows(hot, entries, basis, updated_at, n=3, alert_top=None, links=3, prev=None):
    """Playbook rows from the viral ranking:
    - `style.viral_tricks`: the top `n` viral tricks the Renderer CAN do (value = JSON list), so the
      Watcher picks moments that suit them;
    - `alerts.new_trick`: the best viral trick we can't do yet, if it is in the top `alert_top`
      (default n) overall, with `links` example links and `since` = the first day of this alert
      (kept from `prev`, the previous alerts.new_trick row): the daily skill scout tells the owner
      on the first day and reminds him every few days, never every run."""
    alert_top = alert_top or n
    have = [{"key": t["key"], "name": t["name"], "w": round(t["w"], 2), "a": round(t["a"], 2)}
            for t in hot if t["have"]][:n]
    text = "; ".join(f"{t['name']} (winners {round(t['w'] * 100)}% vs average {round(t['a'] * 100)}%)"
                     for t in have) or "none yet"
    rows = [{"rule_id": "style.viral_tricks", "section": "style",
             "rule": f"viral tricks we can do (pick moments that suit them): {text}",
             "value": json.dumps(have), "source": f"researcher viral tricks ({basis})",
             "updated_at": updated_at}]
    new = next((t for t in hot[:alert_top] if not t["have"]), None)
    if new:
        ranked = sorted(entries, key=lambda e: (_num(e[0].get("breakout")), _num(e[0].get("views"))),
                        reverse=True)
        urls = [e[0].get("url", "") for e in ranked if SKILLS[new["key"]][1](e[1]) and e[0].get("url")][:links]
        try:
            old = json.loads((prev or {}).get("value") or "null") or {}
        except (TypeError, ValueError):
            old = {}
        since = old.get("since") if old.get("key") == new["key"] and old.get("since") else str(updated_at)[:10]
        rule = (f"NEW VIRAL TRICK not built yet: {new['name']} (#{hot.index(new) + 1}, winners "
                f"{round(new['w'] * 100)}% vs average {round(new['a'] * 100)}%, since {since}). Send Claude "
                f"2-3 examples (render notebook cell 6 saves them to Drive _examples, or screen-record): "
                f"{' '.join(urls) or 'see Examples tab'}")
        value = {"key": new["key"], "name": new["name"], "urls": urls, "since": since,
                 "rank": hot.index(new) + 1}
    else:
        rule, value = f"no unbuilt trick in the viral top {alert_top}", None
    rows.append({"rule_id": "alerts.new_trick", "section": "alerts", "rule": rule[:900],
                 "value": json.dumps(value), "source": f"researcher viral tricks ({basis})",
                 "updated_at": updated_at})
    return rows


def viral_from_playbook(rows):
    """style.viral_tricks value -> [{key, name, ...}] (empty when missing)."""
    for r in rows or []:
        if r.get("rule_id") == "style.viral_tricks":
            try:
                return json.loads(r.get("value") or "[]") or []
            except (TypeError, ValueError):
                return []
    return []
