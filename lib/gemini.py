"""Minimal Gemini API client for watching YouTube videos, with a daily call counter.

- Sends a public YouTube URL (optionally several time windows of it, via videoMetadata
  startOffset/endOffset) plus a prompt, and asks for JSON matching a schema.
- Counts every request in data/gemini_usage.json against the daily budget from
  config/settings.json. Gemini's free quota resets at midnight Pacific time, so the
  counter uses the Pacific date. The workflow commits the file after each run.
- Tries each model in the configured list; a 404 (unknown model name) drops it for the run,
  a 503 ("high demand") moves on to the next model for that request. If every model is
  busy, waits `retry_wait` seconds and tries the list once more.
- 503s and per-minute 429s were not served, so they are NOT counted against the daily
  budget; a per-minute 429 waits the time Google asks for (<= 60 s) and retries once.
  A daily 429 stops the run (BudgetReached).
- Requests are paced to `requests_per_minute` (free tier: 5/min for Flash), shared by all
  parallel threads.
- The API key is never printed.
"""
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from lib.config import REPO_ROOT

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
ENV_VAR = "GEMINI_API_KEY"
USAGE_FILE = REPO_ROOT / "data" / "gemini_usage.json"
PACIFIC = ZoneInfo("America/Los_Angeles")


class GeminiError(Exception):
    pass


class BudgetReached(GeminiError):
    pass


class ModelNotFound(GeminiError):
    pass


class Transient(GeminiError):
    """Server busy (500/503) or no answer in time: worth retrying later."""


class Unavailable(Transient):
    """5xx: the request was not served (not counted against the daily budget)."""


class RateLimited(Transient):
    """Per-minute limit (429): wait `wait` seconds and try again."""

    def __init__(self, msg, wait=30.0):
        super().__init__(msg)
        self.wait = wait


def quota_day(now=None):
    now = now or datetime.now(timezone.utc)
    return now.astimezone(PACIFIC).date().isoformat()


class Usage:
    """Daily request counter stored as {"YYYY-MM-DD": {"watcher": n, ...}}."""

    def __init__(self, path=USAGE_FILE, now=None):
        self.path = Path(path)
        self.day = quota_day(now)
        self._lock = threading.Lock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.data = {}

    def used(self, bucket):
        return int(self.data.get(self.day, {}).get(bucket, 0))

    def add(self, bucket, n=1):
        with self._lock:
            day = self.data.setdefault(self.day, {})
            day[bucket] = int(day.get(bucket, 0)) + n
            # keep only the last 14 days
            for d in sorted(self.data)[:-14]:
                del self.data[d]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _http_post(url, body, api_key, timeout=420):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8")).get("error", {})
            msg = f"{e.code} {err.get('status', '')}: {err.get('message', '')}"
        except Exception:
            msg = str(e.code)
        if e.code == 404:
            raise ModelNotFound(msg) from None
        if e.code == 429:
            if is_per_minute(msg):
                raise RateLimited(f"per-minute limit: {msg[:160]}", retry_seconds(msg)) from None
            raise BudgetReached(f"Gemini quota exhausted: {msg}") from None
        if e.code in (500, 502, 503, 504):
            raise Unavailable(msg) from None
        raise GeminiError(msg) from None
    except urllib.error.URLError as e:
        raise GeminiError(f"network error: {e.reason}") from None
    except (TimeoutError, socket.timeout):
        raise Transient(f"no answer within {timeout}s (video too long or Gemini busy)") from None


def is_per_minute(msg):
    m = msg.lower()
    return "per minute" in m or "perminute" in m or "per_minute" in m


def retry_seconds(msg, default=30.0):
    m = re.search(r"retry in ([0-9.]+)s", msg)
    return float(m.group(1)) if m else default


def video_parts(url, windows=None):
    """One part per window ([(start_s, end_s)]), or the whole video if no windows."""
    if not windows:
        return [{"fileData": {"fileUri": url}}]
    return [{"fileData": {"fileUri": url},
             "videoMetadata": {"startOffset": f"{int(s)}s", "endOffset": f"{int(e)}s"}}
            for s, e in windows]


def extract_json(resp):
    try:
        parts = resp["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        reason = (resp.get("promptFeedback") or {}).get("blockReason") or \
            ((resp.get("candidates") or [{}])[0].get("finishReason"))
        raise GeminiError(f"empty answer ({reason})") from None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    try:
        return json.loads(text)
    except ValueError:
        raise GeminiError("answer was not valid JSON") from None


class Gemini:
    def __init__(self, models, daily_budget, bucket, api_key=None, usage=None,
                 http_post=_http_post, retry_wait=30, sleep=time.sleep,
                 requests_per_minute=None, clock=time.monotonic):
        self.api_key = api_key if api_key is not None else os.environ.get(ENV_VAR, "")
        if not self.api_key:
            raise GeminiError(
                f"Secret {ENV_VAR} is missing. Add it in GitHub: Settings > Secrets and "
                "variables > Actions."
            )
        self.models = list(models)
        self.budget = daily_budget
        self.bucket = bucket
        self.usage = usage or Usage()
        self._post = http_post
        self.calls_this_run = 0
        self.retry_wait = retry_wait
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._pace_lock = threading.Lock()
        self._interval = 60.0 / requests_per_minute if requests_per_minute else 0.0
        self._next_ok = 0.0

    def remaining(self):
        return max(self.budget - self.usage.used(self.bucket), 0)

    def generate_json(self, parts, prompt, schema, temperature=0.4):
        """Answer from the first model that serves the request. If all are busy, wait
        `retry_wait` s and go through the list once more (unless retry_wait is None)."""
        rounds = 1 if self.retry_wait is None else 2
        last = None
        for r in range(rounds):
            if r:
                self._sleep(self.retry_wait)
            try:
                return self._try_models(parts, prompt, schema, temperature)
            except Transient as e:
                last = e
        raise last

    def _try_models(self, parts, prompt, schema, temperature):
        body = {
            "contents": [{"role": "user", "parts": list(parts) + [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseSchema": schema, "temperature": temperature,
                                 # fewer tokens per frame: faster answers, less quota
                                 "mediaResolution": "MEDIA_RESOLUTION_LOW"},
        }
        last = None
        for model in list(self.models):
            try:
                return self._one(model, body), model
            except ModelNotFound as e:
                last = e
                print(f"note: model {model} not found, trying the next one")
                with self._lock:
                    if model in self.models:
                        self.models.remove(model)
            except Transient as e:
                last = e  # busy: try the next model for this request
        if isinstance(last, Transient):
            raise last
        raise GeminiError(f"no configured model worked ({last}). Update gemini models in "
                          "config/settings.json")

    def _one(self, model, body):
        for attempt in (0, 1):
            self._pace()
            self._take_slot()
            try:
                return extract_json(self._post(API.format(model=model), body, self.api_key))
            except RateLimited as e:
                self._refund()
                if attempt:
                    raise
                self._sleep(min(e.wait, 60) + 1)
            except Unavailable:
                self._refund()
                raise
            except ModelNotFound:
                self._refund()
                raise

    def _pace(self):
        if not self._interval:
            return
        with self._pace_lock:
            now = self._clock()
            wait = self._next_ok - now
            if wait > 0:
                self._sleep(wait)
            self._next_ok = max(now, self._next_ok) + self._interval

    def _take_slot(self):
        with self._lock:
            if self.remaining() <= 0:
                raise BudgetReached(
                    f"daily Gemini budget used ({self.usage.used(self.bucket)}/{self.budget})")
            self.usage.add(self.bucket)  # count before sending: a timeout may still count
            self.calls_this_run += 1

    def _refund(self):
        with self._lock:
            self.usage.add(self.bucket, -1)
            self.calls_this_run -= 1
