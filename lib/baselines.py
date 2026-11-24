"""How many views a channel's Shorts USUALLY get (its own normal), so a Short can be judged by
how far it beat its own channel ("breakout"), whatever the channel's size.

Per channel: its latest uploads (playlistItems, 1 unit) -> stats of those videos (1 unit per 50)
-> Shorts (<= max_duration_s) at least `min_age_days` old (views have settled) -> their views.
Cached in data/channel_baselines.json for `max_age_days`, committed by the Researcher workflow.
The Short being judged is always left out of its own channel's median.
"""
import json
import statistics
from datetime import datetime, timedelta, timezone

from lib.config import REPO_ROOT

PATH = REPO_ROOT / "data" / "channel_baselines.json"


def _parse(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Baselines:
    def __init__(self, path=None, data=None):
        self.path = path or PATH
        if data is not None:
            self.data = data
        else:
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=0, sort_keys=True) + "\n", encoding="utf-8")

    def stale(self, channel_id, now, max_age_days):
        e = self.data.get(channel_id)
        t = _parse(e.get("at")) if e else None
        return not t or now - t > timedelta(days=max_age_days)

    def refresh(self, yt, channel_ids, now, cfg, parse_duration):
        """Fetch channels whose baseline is missing/old. Stops quietly when the quota budget runs out.
        Returns (refreshed, notes)."""
        todo = [c for c in dict.fromkeys(channel_ids) if c and self.stale(c, now, cfg.get("max_age_days", 3))]
        done, notes = 0, []
        min_age = timedelta(days=cfg.get("min_age_days", 2))
        for ch in todo:
            try:
                ids = yt.recent_uploads(ch, cfg.get("recent_uploads", 30))
                vids = yt.videos(ids, parts="snippet,statistics,contentDetails") if ids else []
            except Exception as e:   # QuotaExceeded or an API error: keep what we have
                notes.append(f"baselines stopped after {done} channels: {str(e)[:120]}")
                break
            views = {}
            for v in vids:
                dur = parse_duration(v.get("contentDetails", {}).get("duration"))
                pub = _parse(v.get("snippet", {}).get("publishedAt"))
                if dur and dur <= cfg.get("max_duration_s", 180) and pub and now - pub >= min_age:
                    views[v["id"]] = int(v.get("statistics", {}).get("viewCount", 0) or 0)
            self.data[ch] = {"views": dict(list(views.items())[: cfg.get("keep", 15)]), "at": _iso(now)}
            done += 1
        return done, notes

    def median(self, channel_id, exclude_id=None, min_shorts=3):
        """Median views of the channel's settled Shorts, without `exclude_id`. None if too few."""
        e = self.data.get(channel_id) or {}
        vals = [v for k, v in (e.get("views") or {}).items() if k != exclude_id]
        return statistics.median(vals) if len(vals) >= min_shorts else None


def breakout(views, median, subscribers, cfg):
    """(ratio, source). Ratio = views / the channel's normal. Without enough of the channel's own
    Shorts, the normal is estimated from subscribers (rough), else a fixed guess."""
    floor = cfg.get("min_baseline", 300)
    if median is not None:
        return views / max(median, floor), "channel"
    if subscribers:
        return views / max(subscribers * cfg.get("fallback_views_per_sub", 0.1), floor), "subscribers"
    return views / max(cfg.get("fallback_views", 5000), floor), "guess"
