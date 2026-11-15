"""Publish a Decider batch (written by Claude in chat) into the `Batch` tab.

Claude writes batches/<YYYY-MM-DD>.json and pushes it on a branch named batch/<date>;
the "Publish batch" workflow runs this script. Every clip is checked before it goes in:
known brand, moment exists and is `ready`, not already in Batch under another clip id
(one clip -> one brand), lengths, title <= 60 chars, base hashtags, credit line and
disclaimer in the description. Valid clips are upserted (approved / fetch_status /
render_status already in the sheet are kept); their Moments rows become `in_batch`.

The owner approves by typing "yes" in the `approved` column.

Batch file format:
{"batch_id": "2026-11-20",
 "clips": [{"moment_id": "...", "brand": "wasted_again", "content_type": "chaos",
            "hook_text": "...", "title_label": "...", "title": "...",
            "description": "...", "hashtags": "#GTA6 #GTA6Shorts #Shorts ...",
            "creator_credit": "@creator"}]}

Run:  python -m agents.publish_batch [batches/2026-11-20.json ...]   (default: all files)
"""
import glob
import json
import sys

from lib import sheets
from lib.config import REPO_ROOT, load_rules, load_settings
from lib.schema import TABS

TAB = "Batch"


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def validate(clip, moment, batch_rows, settings, rules, clip_id):
    """Return a list of problems (empty = OK)."""
    p = []
    brands = {b["key"] for b in settings["brands"]}
    if clip.get("brand") not in brands:
        p.append(f"unknown brand '{clip.get('brand')}' (use one of {sorted(brands)})")
    if clip.get("content_type") not in settings["content_types"]:
        p.append(f"content_type must be one of {settings['content_types']}")
    if moment is None:
        p.append(f"moment {clip.get('moment_id')} not found in Moments")
    elif moment.get("gate_status") not in ("ready", "in_batch"):
        p.append(f"moment {clip.get('moment_id')} is '{moment.get('gate_status')}', not ready")
    for row in batch_rows:
        if row.get("moment_id") == clip.get("moment_id") and row.get("batch_id") != clip_id:
            p.append(f"moment already in Batch as {row.get('batch_id')} ({row.get('brand')})")
    pk, hk, tl = rules["packaging"], rules["hook"], rules["title_label"]
    if len(clip.get("hook_text", "").split()) > hk["max_words"]:
        p.append(f"hook_text longer than {hk['max_words']} words")
    n = len(clip.get("title_label", "").split())
    if not tl["min_words"] <= n <= tl["max_words"]:
        p.append(f"title_label must be {tl['min_words']}-{tl['max_words']} words")
    if not clip.get("title") or len(clip["title"]) > pk["title_max_chars"]:
        p.append(f"title missing or longer than {pk['title_max_chars']} characters")
    tags = clip.get("hashtags", "").lower().split()
    missing = [t for t in pk["base_hashtags"] if t.lower() not in tags]
    if missing:
        p.append(f"hashtags missing {missing}")
    desc = clip.get("description", "")
    if pk["disclaimer"] not in desc:
        p.append("description is missing the disclaimer")
    credit = clip.get("creator_credit", "")
    if not credit or credit not in desc:
        p.append("creator_credit missing or not in the description")
    return p


def build_rows(batch, moments_by_id, batch_rows, settings, rules):
    good, bad = [], []
    for i, clip in enumerate(batch.get("clips", []), start=1):
        clip_id = f"{batch['batch_id']}-{i:02d}"
        m = moments_by_id.get(str(clip.get("moment_id")))
        problems = validate(clip, m, batch_rows, settings, rules, clip_id)
        if problems:
            bad.append((clip_id, problems))
            continue
        good.append({
            "batch_id": clip_id, "moment_id": clip["moment_id"], "video_id": m["video_id"],
            "start_s": int(_f(clip.get("start_s", m["start_s"]))),
            "end_s": int(_f(clip.get("end_s", m["end_s"]))),
            "brand": clip["brand"], "content_type": clip["content_type"],
            "hook_text": clip["hook_text"], "title_label": clip["title_label"],
            "title": clip["title"], "description": clip["description"],
            "hashtags": clip["hashtags"], "creator_credit": clip["creator_credit"],
            "approved": "", "fetch_status": "", "render_status": "",
        })
    return good, bad


def main(paths=None):
    settings, rules = load_settings(), load_rules()
    paths = paths or sorted(glob.glob(str(REPO_ROOT / "batches" / "*.json")))
    if not paths:
        print("No batch files found.")
        return 0
    try:
        client = sheets.get_client()
        ss = sheets.open_sheet(client, settings["sheet"]["id"])
        sheets.ensure_tab(ss, TAB, TABS[TAB])
        b_ws, m_ws = ss.worksheet(TAB), ss.worksheet("Moments")
        batch_rows = sheets.read_records(b_ws)
        moments = {str(m["moment_id"]): m for m in sheets.read_records(m_ws)}
    except sheets.SheetSetupError as e:
        print(f"PUBLISH FAILED (sheet): {e}")
        return 1

    any_bad = False
    for path in paths:
        with open(path, encoding="utf-8") as f:
            batch = json.load(f)
        good, bad = build_rows(batch, moments, batch_rows, settings, rules)
        upd, new = sheets.upsert_rows(b_ws, TABS[TAB], "batch_id", good,
                                      preserve=("approved", "fetch_status", "render_status"))
        if good:
            sheets.set_column(m_ws, TABS["Moments"], "moment_id", "gate_status",
                              {g["moment_id"]: "in_batch" for g in good})
        print(f"{batch['batch_id']}: {new} new clips, {upd} updated, {len(bad)} rejected.")
        for clip_id, problems in bad:
            any_bad = True
            print(f"  REJECTED {clip_id}: " + "; ".join(problems))
    return 1 if any_bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
