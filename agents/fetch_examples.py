"""Download the top example Shorts from the Examples tab to Google Drive (Colab only).

The owner downloads them from Drive and sends them to Claude in chat to build new editing skills.
Order: TOP breakouts first, then the best example of each trick we CAN'T do yet, then the rest.
Saved to <output_dir>/_examples/<date>/NN_<category>_<video_id>.mp4 + _list.txt (what each is).
Never fails the render run: any problem is printed and skipped.

    python -m agents.fetch_examples      (env CLIPPER_EXAMPLES = how many, default 20; 0 = skip)
"""
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from agents.render_batch import FORMAT, cookie_args, is_bot_block
from lib import sheets
from lib.config import load_settings

TOP = "TOP breakouts (any style)"


def _rank(r):
    try:
        return int(r.get("rank") or 99)
    except ValueError:
        return 99


def pick(rows, n):
    """Ordered unique picks: TOP list, then rank-1 of tricks we can't do, then the rest."""
    rows = [r for r in rows if r.get("url")]
    top = sorted([r for r in rows if r.get("category") == TOP], key=_rank)
    missing = sorted([r for r in rows if r.get("category") != TOP and r.get("have_skill") == "no"],
                     key=lambda r: (_rank(r), r.get("category", "")))
    rest = sorted([r for r in rows if r.get("category") != TOP and r.get("have_skill") != "no"],
                  key=lambda r: (_rank(r), r.get("category", "")))
    out, seen = [], set()
    for r in [x for x in missing if _rank(x) == 1] + top + missing + rest:
        if r["url"] not in seen:
            out.append(r)
            seen.add(r["url"])
        if len(out) >= n:
            break
    return out


def video_id(url):
    m = re.search(r"(?:shorts/|v=|youtu\.be/)([\w-]{11})", url or "")
    return m.group(1) if m else ""


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:30] or "example"


def main():
    n = int(os.environ.get("CLIPPER_EXAMPLES", "20") or 0)
    if n <= 0:
        print("Examples: skipped (CLIPPER_EXAMPLES=0).")
        return 0
    settings = load_settings()
    out_root = os.environ.get("CLIPPER_OUTPUT_DIR", settings["render"]["output_dir"])
    try:
        ss = sheets.open_sheet(sheets.get_client(), settings["sheet"]["id"])
        rows = sheets.read_records(ss.worksheet("Examples"))
    except Exception as e:
        print(f"Examples: tab not readable ({str(e)[:120]}); run the Researcher (mode findings) first.")
        return 0
    picks = pick(rows, n)
    if not picks:
        print("Examples: the Examples tab is empty; run the Researcher (mode findings) first.")
        return 0
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    folder = Path(out_root) / "_examples" / day
    folder.mkdir(parents=True, exist_ok=True)
    js = os.environ.get("YTDLP_JS_RUNTIME")
    listing, got = [], 0
    for i, r in enumerate(picks, start=1):
        vid = video_id(r["url"])
        name = f"{i:02d}_{slug(r.get('category'))}_{vid}.mp4"
        path = folder / name
        listing.append(f"{name}\n  {r.get('category')} #{r.get('rank')} | we can do it: {r.get('have_skill')}\n"
                       f"  {r['url']} | {r.get('views')} views, {r.get('breakout')}x its channel\n"
                       f"  style: {r.get('style')}\n")
        if path.exists():
            got += 1
            continue
        cmd = [sys.executable, "-m", "yt_dlp", "-f", FORMAT, "--merge-output-format", "mp4",
               "--no-playlist", "--no-warnings", "--no-progress", "-o", str(path)]
        if js:
            cmd += ["--js-runtimes", js]
        cmd += cookie_args() + [r["url"]]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode == 0 and path.exists():
            got += 1
            continue
        msg = ((p.stderr or p.stdout).strip().splitlines() or ["download failed"])[-1]
        print(f"  {name}: {msg[:120]}")
        if is_bot_block(msg):
            print("  YouTube's bot check: stopping the example downloads (restart the runtime and run again).")
            break
    (folder / "_list.txt").write_text("\n".join(listing), encoding="utf-8")
    print(f"Examples: {got}/{len(picks)} Shorts in Google Drive -> {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
