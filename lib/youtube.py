"""Small YouTube Data API v3 client with quota counting.

Costs (units): search.list = 100, videos/channels/playlistItems.list = 1 per call
(up to 50 ids per call). Every call is counted; a call that would go over the run's
budget raises QuotaExceeded *before* it is made.

The API key is never printed: errors show only the endpoint name and Google's message.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

API = "https://www.googleapis.com/youtube/v3/"
ENV_VAR = "YOUTUBE_API_KEY"
COST = {"search": 100, "videos": 1, "channels": 1, "playlistItems": 1, "commentThreads": 1}


class YouTubeError(Exception):
    pass


class QuotaExceeded(YouTubeError):
    pass


def _http_get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
            err = body.get("error", {})
            reason = (err.get("errors") or [{}])[0].get("reason", "")
            msg = f"{e.code} {reason}: {err.get('message', '')}"
        except Exception:
            msg = str(e.code)
        raise YouTubeError(msg) from None
    except urllib.error.URLError as e:
        raise YouTubeError(f"network error: {e.reason}") from None


def chunks(items, n=50):
    items = list(items)
    for i in range(0, len(items), n):
        yield items[i : i + n]


class YouTube:
    def __init__(self, api_key=None, budget_units=1000, http_get=_http_get):
        self.api_key = api_key if api_key is not None else os.environ.get(ENV_VAR, "")
        if not self.api_key:
            raise YouTubeError(
                f"Secret {ENV_VAR} is missing. Add it in GitHub: Settings > Secrets and "
                "variables > Actions."
            )
        self.budget = budget_units
        self.used = 0
        self._get = http_get

    def _call(self, endpoint, params):
        cost = COST[endpoint]
        if self.used + cost > self.budget:
            raise QuotaExceeded(
                f"run budget reached ({self.used}/{self.budget} units); skipping {endpoint}"
            )
        query = dict(params, key=self.api_key)
        url = API + endpoint + "?" + urllib.parse.urlencode(query)
        self.used += cost  # Google charges even for failed calls
        try:
            return self._get(url)
        except YouTubeError as e:
            raise YouTubeError(f"{endpoint}: {e}") from None

    # --- endpoints -------------------------------------------------------------

    def search_video_ids(self, q, published_after, relevance_language=None,
                         event_type=None, order="viewCount", max_results=50):
        params = {
            "part": "id",
            "type": "video",
            "q": q,
            "order": order,
            "publishedAfter": published_after,
            "maxResults": max_results,
        }
        if relevance_language:
            params["relevanceLanguage"] = relevance_language
        if event_type:
            params["eventType"] = event_type
        data = self._call("search", params)
        return [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]

    def videos(self, ids, parts="snippet,statistics,contentDetails,liveStreamingDetails"):
        out = []
        for batch in chunks(dict.fromkeys(ids)):
            data = self._call("videos", {"part": parts, "id": ",".join(batch), "maxResults": 50})
            out.extend(data.get("items", []))
        return out

    def channels(self, ids, parts="snippet,statistics"):
        out = []
        for batch in chunks(dict.fromkeys(ids)):
            data = self._call("channels", {"part": parts, "id": ",".join(batch), "maxResults": 50})
            out.extend(data.get("items", []))
        return out

    def top_comments(self, video_id, max_results=100):
        """Top-level comments by relevance: [(text, like_count)]. 1 unit.
        Returns [] when comments are disabled."""
        try:
            data = self._call("commentThreads", {
                "part": "snippet", "videoId": video_id, "order": "relevance",
                "maxResults": max_results, "textFormat": "plainText",
            })
        except YouTubeError as e:
            if "commentsDisabled" in str(e):
                return []
            raise
        out = []
        for it in data.get("items", []):
            sn = it["snippet"]["topLevelComment"]["snippet"]
            out.append((sn.get("textDisplay", ""), int(sn.get("likeCount", 0) or 0)))
        return out

    def recent_uploads(self, channel_id, max_results=10):
        """Latest uploads of a channel via its uploads playlist (UC... -> UU...)."""
        if not channel_id.startswith("UC"):
            return []
        data = self._call("playlistItems", {
            "part": "contentDetails",
            "playlistId": "UU" + channel_id[2:],
            "maxResults": max_results,
        })
        return [it["contentDetails"]["videoId"] for it in data.get("items", [])]
