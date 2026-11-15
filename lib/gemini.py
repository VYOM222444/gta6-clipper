"""Minimal Gemini API client for watching YouTube videos, with a daily call counter.

- Sends a public YouTube URL (optionally several time windows of it, via videoMetadata
  startOffset/endOffset) plus a prompt, and asks for JSON matching a schema.
- Counts every request in data/gemini_usage.json against the daily budget from
  config/settings.json. Gemini's free quota resets at midnight Pacific time, so the
  counter uses the Pacific date. The workflow commits the file after each run.
- Tries each model in the configured list; a 404 (unknown model name) moves to the next.
- The API key is never printed.
"""
import json
import os
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
            raise BudgetReached(f"Gemini quota exhausted: {msg}") from None
        if e.code in (500, 502, 503, 504):
            raise Transient(msg) from None
        raise GeminiError(msg) from None
    except urllib.error.URLError as e:
        raise GeminiError(f"network error: {e.reason}") from None
    except (TimeoutError, socket.timeout):
        raise Transient(f"no answer within {timeout}s (video too long or Gemini busy)") from None


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
                 http_post=_http_post, retry_wait=20, sleep=time.sleep):
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
        self._lock = threading.Lock()

    def remaining(self):
        return max(self.budget - self.usage.used(self.bucket), 0)

    def generate_json(self, parts, prompt, schema, temperature=0.4):
        """One retry after a short wait if Gemini is busy (each attempt counts)."""
        try:
            return self._generate_once(parts, prompt, schema, temperature)
        except Transient:
            if self.retry_wait is None:
                raise
            self._sleep(self.retry_wait)
            return self._generate_once(parts, prompt, schema, temperature)

    def _take_slot(self):
        with self._lock:
            if self.remaining() <= 0:
                raise BudgetReached(
                    f"daily Gemini budget used ({self.usage.used(self.bucket)}/{self.budget})")
            self.usage.add(self.bucket)  # count before sending: failed calls can still count
            self.calls_this_run += 1

    def _generate_once(self, parts, prompt, schema, temperature):
        body = {
            "contents": [{"role": "user", "parts": list(parts) + [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseSchema": schema, "temperature": temperature,
                                 # fewer tokens per frame: faster answers, less quota
                                 "mediaResolution": "MEDIA_RESOLUTION_LOW"},
        }
        last = None
        while self.models:
            model = self.models[0]
            self._take_slot()
            try:
                return extract_json(self._post(API.format(model=model), body, self.api_key)), model
            except ModelNotFound as e:
                last = e
                print(f"note: model {model} not found, trying the next one")
                with self._lock:
                    if self.models and self.models[0] == model:
                        self.models.pop(0)
        raise GeminiError(f"no configured model worked ({last}). Update gemini models in "
                          "config/settings.json")
