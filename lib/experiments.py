"""Experiment report: do the skills under test (shake, freeze, split, ...) get more views on OUR
posts? Compares our YouTube posts WITH vs WITHOUT each skill at the earliest checkpoint where
both sides have enough posts: views_24h, else views_12h, else views_6h (so the first answer comes
within hours, and firms up as more clips are posted). Median views, not average (one viral clip
must not decide). Written to Playbook rows `experiments.<skill>` by agents/log_posts.py.
"""
import re
import statistics

CHECKPOINTS = ("views_24h", "views_12h", "views_6h")


def tags_of(template_version):
    """'2+zoom2,impact,shake,split|phrase-middle,hook' -> {'zoom2','impact','shake','split',...}"""
    return {t for t in re.split(r"[+,|]", str(template_version or "")) if t}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compare(posts, skill, min_n=3):
    """posts: [(tags, row)] -> dict for one skill, or None when not enough posts yet."""
    for cp in CHECKPOINTS:
        on = [_num(r.get(cp)) for t, r in posts if skill in t and _num(r.get(cp)) is not None]
        off = [_num(r.get(cp)) for t, r in posts if skill not in t and _num(r.get(cp)) is not None]
        if len(on) >= min_n and len(off) >= min_n:
            m_on, m_off = statistics.median(on), statistics.median(off)
            ratio = (m_on + 1) / (m_off + 1)
            return {"skill": skill, "checkpoint": cp, "n_on": len(on), "n_off": len(off),
                    "median_on": m_on, "median_off": m_off, "ratio": ratio}
    return None


def verdict(c, sure_n=10, better=1.3):
    early = min(c["n_on"], c["n_off"]) < sure_n
    if c["ratio"] >= better:
        v = "LOOKS BETTER" if early else "BETTER"
    elif c["ratio"] <= 1 / better:
        v = "LOOKS WORSE" if early else "WORSE"
    else:
        v = "no clear difference"
    return v + (" (early, few posts)" if early else "")


def report(posted_rows, batch_rows, skills, min_n=3):
    """-> [compare dict + 'verdict'] for each skill with enough YouTube posts."""
    tv = {b.get("batch_id"): tags_of(b.get("template_version")) for b in batch_rows}
    posts = [(tv.get(r.get("batch_id"), set()), r) for r in posted_rows
             if r.get("platform") == "youtube" and r.get("batch_id") in tv]
    out = []
    for s in skills:
        c = compare(posts, s, min_n)
        if c:
            c["verdict"] = verdict(c)
            out.append(c)
    return out


def playbook_rows(results, updated_at):
    rows = []
    for c in results:
        hours = c["checkpoint"].replace("views_", "")
        rows.append({"rule_id": f"experiments.{c['skill']}", "section": "experiments",
                     "rule": f"{c['skill']}: {c['verdict']} - median {int(c['median_on'])} views at {hours} with it "
                             f"({c['n_on']} posts) vs {int(c['median_off'])} without ({c['n_off']} posts)",
                     "value": f"{c['ratio']:.2f}", "source": "log_posts experiment report",
                     "updated_at": updated_at})
    return rows
