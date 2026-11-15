# GTA6 Clipper — Project Guide for Claude Code

Read this whole file at the start of every session. Update it at the end of every session
with what changed (new files, decisions, known issues).

## 1. What this project is

An automated system that finds trending GTA 6 content, picks the best moments from other
creators' videos, turns them into vertical Shorts, and learns from results. GTA 6 launched
on Nov 19, 2026. The owner is Vyom, a Class 11 student building this solo around school and
JEE prep, so the system must run on its own and stay cheap and simple.

Principle: lean build-measure-learn. Ship the smallest working version first, then improve.

## 2. Hard constraints

- **Free only.** YouTube Data API (10,000 units/day), Gemini API free tier, GitHub Actions
  free minutes, free Colab. No paid APIs, no paid hosting.
- **The owner's laptop is a 32-bit machine with 2 GB RAM.** Nothing runs locally. Everything
  runs on GitHub Actions or Google Colab. Never suggest installing tools locally.
- **The owner usually runs Claude Code from the Claude app on an iPhone 13 Pro.** Keep
  instructions short and phone-friendly. Steps that need a bigger screen (Google Cloud
  console clicks, uploading files, running Colab notebooks) happen on the laptop's browser:
  say clearly when a step is better done on the laptop.
- **Secrets** live in GitHub Secrets only: `YOUTUBE_API_KEY`, `GEMINI_API_KEY`,
  `GOOGLE_SERVICE_ACCOUNT_JSON`. Never print, log, commit or hard-code keys.
- **Gemini quota is very limited.** As of Nov 2026 the free Flash models allow only about
  20 requests/day per project; Flash-Lite models allow more (reported ~500/day). Google no
  longer publishes exact numbers: check AI Studio for the real limits. Budget (in
  `config/settings.json`): ~15 calls/day for the Watcher on Flash, ~5 kept for retries;
  bulk tagging (Researcher) on Flash-Lite. Always filter with free signals first (Scout,
  Tracker, Signals) and send Gemini only the top candidates, ideally only short windows
  around a signal, not whole videos. Ask for 3–5 moments per call. Count every call and stop
  cleanly before the cap. Read limits from config, never assume.
- **YouTube downloads may be blocked on GitHub's servers.** Downloading (yt-dlp, including
  chat replays and audio) and video rendering run in Colab if GitHub is blocked.

## 3. Channels (brands)

All content is clipped from other creators. One clip goes to exactly ONE brand, which posts
it on its YouTube + Instagram (+ Facebook later). Never the same clip on two brands.

| Brand | YouTube handle | Instagram |
|---|---|---|
| Wasted Again | @wastedagainclips | wastedagain.clips |
| Neon Wanted | @neonwanted.clips | neonwanted.clips |
| Mission Failed | @missionfailed.clips | missionfailed.clips |
| Neon Getaway | @neongetaway.clips | neongetaway.clips |
| Five Star Chaos | @fivestarchaos.clips | fivestarchaos.clips |

All brands post mixed content. Every clip gets a content-type tag:
`news`, `chaos`, `reaction`, `secret`, `tip`. Batch goal: 20 clips (4 per brand), scaled up
gradually.

## 4. Architecture

- **GitHub repo** (this one): all code, config, the Playbook, raw data CSVs.
- **GitHub Actions**: scheduled jobs (Scout, Tracker, Researcher data collection, Gemini calls).
- **Google Sheet "GTA6 Clipper"**: the human-readable dashboard.
  Sheet ID: `1hq8aaJVeoPyUzQv01V1zJwxAMYvVIee5fx1ri33el9E`
  Tabs: `Candidates`, `Tracker`, `Moments`, `Playbook`, `Batch`, `Posted`, `Research`.
  Column layout of every tab lives in `lib/schema.py` (append new columns at the END only).
  Written via a Google service account (key in `GOOGLE_SERVICE_ACCOUNT_JSON`; sheet already
  shared with the service account as Editor). All sheet access goes through `lib/sheets.py`.
- **Raw snapshot log**: `data/snapshots.csv` (append-only, never overwrite rows). The sheet
  only gets a summary (one row per video, latest numbers + status).
- **Colab**: Fetcher (yt-dlp segment downloads), Renderer (ffmpeg), and Signals steps that
  need downloads if GitHub is blocked. Notebooks pull code from this repo. Colab signs in
  with the owner's own Google account (no service account needed there).
- **Claude in chat** (the owner's chat with Claude): reads the sheet, acts as Decider and
  Analyst, designs style templates, maintains Playbook rules.
- **Gemini**: watches videos from YouTube links (no download) and returns structured JSON.
- **Hugging Face**: source of free open models run inside Colab (Whisper for captions). Its
  hosted API is not free; don't use it.

Design rules:
1. One job per agent, one file (or small module) per agent.
2. Fixed input/output formats (documented below). Agents never call each other directly;
   they communicate through the sheet / CSV files.
3. Use AI only where judgment is needed. Everything else is plain Python.
4. Every agent can be re-run on its own and is safe to re-run (idempotent, skips done work).
5. Human checkpoints: owner approves the batch before fetching, and reviews every clip
   before posting.

## 5. The agents (in pipeline order)

The **Playbook** (`playbook/PLAYBOOK.md` + `playbook/rules.json`) is read by every agent.

1. **Researcher** — studies top GTA Shorts (videos, not channels) and writes the Playbook.
   Compares winners vs average Shorts (never winners alone). Funnel: ~500 Shorts metadata
   (free) → top 50 + 50 average deep-analyzed by Gemini (Flash-Lite) → stats summary →
   Playbook rules. Analyze blind first (no view counts given to Gemini), then join with numbers.
   Also records titles, hashtags and description styles of winners → SEO rules.
   Labels a Short winner/average/low only after it is 3–7 days old.
   *v0: the hand-written starter Playbook. Automated in Phase 4.*
2. **Scout** — finds trending GTA 6 long videos, live-stream VODs and Shorts via the YouTube
   Data API. Ranks by views/hour and outlier score (views ÷ channel subscribers). Checks the
   Creator Watchlist (`config/watchlist.json`) for new uploads. Works with the Researcher via
   source mapping (link viral Shorts back to the long video they were clipped from) and a
   saturation check (skip moments already clipped many times). Output → `Candidates` tab.
   Search is expensive (100 units); stats lookups are cheap (1 unit per 50 videos).
   **Campaign lead scan:** scans video descriptions and channel "About" text for clipping
   campaign signs (keywords + link domains in `playbook/rules.json` → `campaign_lead`) and
   flags `campaign_lead` + the link. The owner verifies leads weekly and adds real campaigns
   to `watchlist.json` (rate + rules); Scout ranks campaign creators first.
3. **Tracker** — snapshots every tracked video every 6 hours. Computes age, velocity
   (views/hour since last snapshot), acceleration (velocity ratio), engagement, size-adjusted
   reach. Labels: `New` (<6h), `Rising`, `Breakout`, `Fading`, `Mature` (7 days, final label).
   Thresholds live in `playbook/rules.json`. Stops tracking after 7 days or 3 fading
   snapshots. Raw → `data/snapshots.csv`; summary → `Tracker` tab.
4. **Signals** — finds *where* in a candidate video the moments are, with zero Gemini calls:
   - **Chat spikes** (main signal for live-stream VODs): download the chat replay with
     yt-dlp (`--skip-download --write-subs --sub-langs live_chat`), count reaction messages
     ("LMAO", "💀", "CLIP IT", "NO WAY", "W", "😂"…) per 10-second window, flag windows far
     above the stream's baseline. Chat reacts ~20 s after the moment, so the search window
     starts ~30 s before the spike. Only works for live-stream VODs with chat replay.
   - **Loudness peaks** (fallback, any video): audio-only download, ffmpeg loudness per
     second, flag spikes (screams, hype). Runs in Colab.
   - **Most Replayed** peaks (via yt-dlp metadata, not always available) and timestamped
     comments ("3:45 💀").
   Output: candidate windows `{video_id, start, end, signal, strength}` → `Moments` (source
   `signal`). Word lists and thresholds live in `playbook/rules.json`.
5. **Watcher (Gemini)** — watches the top candidates by YouTube URL, limited to the Signals
   windows when available (whole video only for short videos with no signals). Returns
   moments: `{start, end, reason, content_type, style_tags, suggested_brand, has_promo}`,
   3–5 per call. Supports prompt-to-clip ("every police chase"). Skips sponsor/promo
   segments. Output → `Moments`.
6. **Gatekeeper** — blocks: leaked footage, creators on the blocklist
   (`config/blocklist.json`), moments already used (`Posted` tab), promo segments,
   duplicates across brands.
7. **Decider (Claude in chat)** — scores approved moments, assigns each to one brand, writes
   hook text, top title label, title, description and hashtags. Output → `Batch` tab, which
   the Fetcher reads.
8. **Fetcher (Colab)** — downloads only the chosen segments (`yt-dlp --download-sections`)
   to Google Drive, one folder per brand.
9. **Renderer (Colab)** — Whisper word timestamps (model from Hugging Face), crop/reframe to
   9:16 (gameplay-aware; split-screen for streamer facecam), top title label, word-by-word
   captions with keyword highlights, hook text, creator credit, audio normalization, brand
   style template (`templates/<brand>.json`, versioned). Saves MP4 + a `.txt` with title/
   description/hashtags to Drive. Reuse code from OpenShorts (MIT license,
   github.com/mutonby/openshorts) for reframing and captions instead of writing from
   scratch; keep its license notice. PeakCut (MIT, open-sourced Nov 2026, unproven) is a
   possible second reference.
10. **Publisher (owner)** — reviews on iPhone, posts, adds trending sound in-app, credits
    the creator, logs the link in `Posted`.
11. **Analyst (Claude in chat)** — reads results at 24h and 7 days, compares brands, styles
    and YouTube vs Instagram, feeds lessons back to the Playbook.

## 6. Safety rules (enforce in code where possible)

- Prefer creators who allow clipping (clipping campaigns count as permission); respect the
  blocklist; follow each campaign's own rules and disclosure settings.
- Always credit the source creator in the description.
- Every clip must be transformed (hook text, captions, edits), never a raw repost. This also
  matters for YouTube's "reused content" monetization rule.
- No leaked or pre-release footage. No watermark removal.
- Never post the same clip on two brands.
- Music is added inside the apps, not baked into the video.

## 7. Repo layout

```
CLAUDE.md
requirements.txt            # pinned deps
playbook/PLAYBOOK.md        # human-readable rules
playbook/rules.json         # machine-readable thresholds/weights/word lists
config/settings.json        # quotas (incl. Gemini budget), schedules, search terms, sheet ID, brands
config/watchlist.json       # creators to check (+ campaign info)
config/blocklist.json       # creators/channels never to clip
lib/config.py               # load_settings / load_rules / load_watchlist / load_blocklist  [S1]
lib/schema.py               # column layout of every sheet tab                            [S1]
lib/sheets.py               # service-account login, ensure_tab, read/append helpers      [S1]
agents/setup_sheet.py       # creates/repairs the 7 tabs (safe to re-run)                 [S1]
agents/scout.py             # S2
agents/tracker.py           # S3
agents/signals.py
agents/watcher.py
agents/gatekeeper.py
agents/researcher.py
lib/youtube.py  lib/gemini.py  lib/storage.py
templates/<brand>.json
colab/fetch_and_render.ipynb
colab/signals.ipynb         # if chat/audio downloads are blocked on GitHub
data/snapshots.csv
.github/workflows/setup-sheet.yml   # manual "Setup sheet" button                         [S1]
.github/workflows/tests.yml         # runs pytest on every PR / push to main              [S1]
tests/                              # pytest; tests/fakes.py = in-memory sheet fakes      [S1]
```

## 8. Build roadmap

- Phase 0 (Nov 14): keys, repo, sheet, this file. DONE.
- Phase 1 (Nov 15–18): S1 repo structure + sheet tabs + connection test (code DONE);
  S2 Scout (with campaign lead scan); S3 Tracker on a 6-hour schedule.
  Goal: collecting data at launch.
- Phase 2 (Nov 19–~25): Signals (chat spikes first, loudness fallback), Watcher on Signals
  windows, Decider handoff (`Batch` tab), Fetcher + Renderer in Colab, one template.
  Post first clips.
- Phase 3: daily batches, Gatekeeper, 5 templates, gameplay reframing, split-screen.
- Phase 4: automated Researcher, Most Replayed + timestamped comments, source mapping +
  saturation check, our own virality score from Tracker data, sound effects library.
- Phase 5: Analyst loop, template versioning comparisons, YouTube auto-post via API
  (uploads from unverified API apps stay private until Google audits the app), scale to
  20-clip batches.

## 9. How to work in this repo

- One agent or feature per session. Keep sessions small.
- Run agents from the repo root as modules: `python -m agents.<name>`.
- Write tests for pure logic (scoring, labels, parsing). Mock API calls in tests
  (see `tests/fakes.py`). Run `python -m pytest -q`.
- Run the code to check it works before finishing. Explain any manual step the owner must
  do (clicks in Google Cloud, sharing the sheet) in plain, numbered steps.
- Ship via a branch + pull request; the owner merges from the GitHub app.
- Log API quota usage per run.
- Keep dependencies minimal and pinned in `requirements.txt`.
- At the end of a session, update the "Status / log" section below.

## 10. Status / log

- 2026-11-14: Repo created. Secrets `YOUTUBE_API_KEY` and `GEMINI_API_KEY` added. Google
  Sheet created. Instagram accounts created; YouTube: Wasted Again created, other 4 pending.
- 2026-11-15: Sheets + Drive APIs enabled; service account created; secret
  `GOOGLE_SERVICE_ACCOUNT_JSON` added; sheet shared with the service account as Editor.
  Decisions: keep the service-account plan (not CSV-only); Gemini budget set for the
  ~20/day free cap; Signals moved to Phase 2 with chat-spike + loudness detection (learned
  from 5 reference reels on GTA clipping tools); Scout gets a campaign lead scan; top title
  label added to clip format. Higgsfield and Hugging Face hosted API rejected (not free).
- 2026-11-15 (S1): Skeleton built: config files, `lib/config.py`, `lib/schema.py`,
  `lib/sheets.py`, `agents/setup_sheet.py`, 15 tests passing, workflows `setup-sheet.yml`
  (manual) and `tests.yml`. Pending: owner merges the PR and runs "Setup sheet" to confirm
  the live connection. Known: Gemini model names in `settings.json` are aliases
  (`gemini-flash-latest`, `gemini-flash-lite-latest`); verify in S4.
