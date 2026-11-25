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
- **GitHub Actions minutes:** private repo = 2,000 free min/month. Scout + Tracker ~250,
  Signals (comments only) ~60, Watcher (2/day, 4 parallel Gemini calls) ~500. Keep new schedules small.
- **YouTube downloads may be blocked on GitHub's servers.** Downloading (yt-dlp, including
  chat replays and audio) and video rendering run in Colab if GitHub is blocked.

## 3. Channels (brands)

All content is clipped from other creators. One clip goes to exactly ONE brand, which posts
it on its YouTube (owner uploads) + Instagram + Facebook (auto-posted via the Meta API by
`agents/post_social.py` once set up). Never the same clip on two brands.

| Brand | key | YouTube handle | Instagram |
|---|---|---|---|
| Wasted Again | wasted_again | @wastedagainclips | wastedagain.clips |
| Neon Wanted | neon_wanted | @neonwanted_clips | neonwanted.clips |
| Busted Again | busted_again | @bustedagainclips | bustedagain.clips (rename from missionfailed.clips) |
| Neon Getaway | neon_getaway | @neongetaway_clips | neongetaway.clips |
| Five Star Chaos | five_star_chaos | @fivestarchaos_clips | fivestarchaos.clips |

All brands post mixed content. Every clip gets a content-type tag:
`news`, `chaos`, `reaction`, `secret`, `tip`. Batch goal: 20 clips (4 per brand), scaled up
gradually.

## 4. Architecture

- **GitHub repo** (this one): all code, config, the Playbook, raw data CSVs.
- **GitHub Actions**: scheduled jobs (Scout, Tracker, Researcher data collection, Gemini calls).
- **Google Sheet "GTA6 Clipper"**: the human-readable dashboard.
  Sheet ID: `1hq8aaJVeoPyUzQv01V1zJwxAMYvVIee5fx1ri33el9E`
  Tabs: `Candidates`, `Tracker`, `Moments`, `Playbook`, `Batch`, `Posted`, `Research`,
  `Keywords`.
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
5. Human checkpoint (decided 2026-11-17): batches are auto-approved
   (`settings.batch.auto_approve`), so the render runs without a click. Safety comes from
   the Gatekeeper + the owner glancing at every clip while uploading it to YouTube (skip
   anything wrong; "no" in `approved` stops a clip before rendering). Instagram auto-post
   (when built) only posts after a veto window.

## 5. The agents (in pipeline order)

The **Playbook** (`playbook/PLAYBOOK.md` + `playbook/rules.json`) is read by every agent.

1. **Researcher** — studies top GTA Shorts (videos, not channels) and writes the Playbook.
   Compares winners vs average Shorts (never winners alone). Funnel: ~500 Shorts metadata
   (free) → top 50 + 50 average deep-analyzed by Gemini (Flash-Lite) → stats summary →
   Playbook rules. Analyze blind first (no view counts given to Gemini), then join with numbers.
   Also records titles, hashtags and description styles of winners → SEO rules.
   Labels a Short winner/average/low only after it is 3–7 days old.
   *Built 2026-11-17 (`agents/researcher.py`, daily): collect -> analyze -> findings, see log.*
   **Fresh tier** (2026-11-19): also Shorts 0-72 h old ("what works right now"), ranked by
   views/hour vs Shorts of the same age, incl. Scout's Shorts; findings `research.now.*`
   (SHIFT = differs from the 7-day verdict); hot topic words feed Scout's ranking.
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
6. **Gatekeeper** (`agents/gatekeeper.py`, after every Watcher run) — re-checks `ready`
   moments: rejected_leak (leak / pre-release words), rejected_blocklist (channel id or
   name in `config/blocklist.json`), rejected_used (Batch/Posted), rejected_duplicate
   (overlapping weaker moment of the same video), rejected_saturated (other Shorts that
   reference the source and share >= 2 keywords with the moment already have >= 20k views).
   Notes in Moments `saturation` / `gate_note`. Promo is gated by the Watcher.
7. **Decider** — automatic every evening (`agents/decider.py`, ~18:00 IST): picks ready
   moments (quality + Tracker label - age - partly-clipped), assigns ONE brand each
   (Python), writes hook / label / title / description / hashtags / ig_caption with one
   Gemini text call (fallback text if Gemini fails), validates and writes the `Batch` tab
   (auto-approved) + `batches/<date>.json`. Claude in chat can still make or fix a batch by
   hand (procedure below).
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
requirements-signals.txt    # + yt-dlp[default] (Colab Signals notebook)
requirements-render.txt     # + yt-dlp[default] + faster-whisper (Colab Render notebook) [C]
playbook/PLAYBOOK.md        # human-readable rules
playbook/rules.json         # machine-readable thresholds/weights/word lists
config/settings.json        # quotas (incl. Gemini budget), schedules, search terms, sheet ID, brands
config/watchlist.json       # creators to check (+ campaign info)
config/blocklist.json       # creators/channels never to clip
lib/config.py               # load_settings / load_rules / load_watchlist / load_blocklist  [S1]
lib/schema.py               # column layout of every sheet tab                            [S1]
lib/sheets.py               # service-account login, ensure_tab, read/append/upsert (RAW)  [S1, A]
lib/youtube.py              # YouTube Data API client, per-run quota budget, key never logged [A]
agents/setup_sheet.py       # creates/repairs the 7 tabs (safe to re-run)                 [S1]
agents/scout.py             # searches (EN + Hindi + finished streams) -> Candidates      [A]
agents/tracker.py           # 6-hourly snapshots -> data/snapshots.csv + Tracker tab       [A]
agents/signals.py           # SIGNALS_MODE=api (GitHub: comments) / full (Colab: + chat, Most Replayed) [B1,B2]
agents/watcher.py           # Gemini on Signals windows / short videos -> Moments (ready/rejected_*) [B2]
agents/publish_batch.py     # batches/<date>.json -> Batch tab, validated                 [B2]
agents/decider.py           # automatic evening batch -> Batch tab + batches/<date>.json   [D]
.github/workflows/decider.yml       # scheduler ~18:00 IST + backup cron 12:53 UTC; guard; commits batch [D]
data/gemini_usage_decider.json      # Decider's own Gemini counter                         [D]
batches/<date>.json         # Decider output, pushed on branch batch/<date>               [B2]
agents/log_posts.py         # posts/<date>.json -> Posted tab; YouTube stats of our posts  [P]
posts/<date>.json           # links the owner posted, pushed on branch posts/<date>       [P]
lib/keywords.py             # trending phrases from Scout titles -> Keywords tab (Scout run) [L]
agents/gatekeeper.py        # leaks, blocklist, used, duplicates, saturation -> Moments gate_status [G]
.github/workflows/gatekeeper.yml    # started by the Watcher (chain) + manual             [G]
agents/researcher.py        # daily: GTA 6 Shorts 3-14 d old + fresh 0-72 h -> Research tab; Flash-Lite blind tags; findings -> Playbook tab [R]
data/gemini_usage_researcher.json # Researcher's own Gemini counter; committed by its workflow [R]
.github/workflows/researcher.yml    # daily 01:23 UTC (06:53 IST, all) + 11:23 UTC (16:53 IST, fresh only) + manual [R]
lib/gemini.py               # Gemini generateContent + YouTube windows, daily counter, model fallback [B2]
lib/edits.py                # editing pack: payoff/hits from audio, zooms, SFX, slow-mo replay, end cut, Demucs [E]
lib/render.py               # caption chunks, keyword highlight, .ass builder, ffmpeg command, post .txt [C]
agents/render_batch.py      # Fetcher + Renderer: approved Batch rows -> Drive mp4 + txt  [C]
templates/<brand>.json      # v1 style per brand: accent colour, fonts, sizes, positions  [C]
colab/render.ipynb          # evening Colab run: download + render approved clips        [C]
colab/signals.ipynb         # morning Colab run: chat spikes + Most Replayed (yt-dlp works there) [B2]
data/snapshots.csv          # append-only; committed by the Tracker workflow              [A]
data/gemini_usage.json      # Gemini calls per Pacific day; committed by the Watcher workflow [B2]
.github/workflows/setup-sheet.yml   # manual "Setup sheet" button                         [S1]
.github/workflows/scout.yml         # scheduler + backup cron :37/6h + manual; guard; starts Tracker [A, Rel]
.github/workflows/tracker.yml       # started by Scout + manual; commits snapshots.csv; starts Signals [A, Rel]
.github/workflows/signals.yml       # comments only; started by Tracker + manual; starts Watcher [B2, Rel]
.github/workflows/watcher.yml       # started by Signals + backup cron 12:47 UTC; guard; commits usage [B2, Rel]
tools/apps_script_trigger.gs        # Google Apps Script: reliable 6-hourly / daily starts   [Rel]
.github/workflows/publish-batch.yml # on push to batch/** branches                        [B2]
.github/workflows/log-posts.yml     # on posts/** pushes (main or posts/** branches)      [P]
.github/workflows/tests.yml         # runs pytest on every PR / push to main              [S1]
.github/workflows/social.yml        # started by Tracker + manual (dry run); Instagram + Facebook posts [IG]
agents/post_social.py       # rendered Batch rows -> Instagram Reel + Facebook Page Reel after veto window [IG]
lib/experiments.py          # our posts with vs without each skill -> Playbook experiments.*  [X]
LiveChat tab                # live chat reactions per 10 s, written by Apps Script pollLive  [Live]
tests/                              # pytest; tests/fakes.py = in-memory sheet fakes      [S1]
```

## 8. Build roadmap

- Phase 0 (Nov 14): keys, repo, sheet, this file. DONE.
- Phase 1 (Nov 15–18): S1 repo structure + sheet tabs + connection test (DONE);
  Chunk A = Scout (with campaign lead scan + Hindi search) + Tracker on a 6-hour schedule.
  Goal: collecting data at launch.
- Build is grouped in chunks: A (Scout + Tracker), B (Signals + Watcher + Decider
  handoff, after real launch data), C (Fetcher + Renderer in Colab + first template).
- Phase 2 (Nov 19–~25, chunks B and C): Signals (chat spikes first, loudness fallback), Watcher on Signals
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

## 10. Daily schedule (IST) and the Decider procedure

Automatic (since 2026-11-17 GitHub cron is only a backup; it skipped ~half the runs):
- **Pipeline chain** every ~6 h: Scout -> (dispatches) Tracker -> Signals (comments) ->
  Watcher -> Gatekeeper.
  Started by the Apps Script scheduler (`tools/apps_script_trigger.gs`, every 6 h) and by
  Scout's own backup cron (:37 every 6 h UTC). Guard job: a scheduled / scheduler start is
  skipped if the main job really ran < 5 h ago (Watcher < 2 h, Researcher < 20 h). The
  manual "Run workflow" button and chain starts always run.
- Watcher backup cron 12:47 UTC (18:17 IST). Researcher daily ~07:00 IST (scheduler) +
  backup cron 01:23 UTC; fresh-tier run 11:23 UTC (16:53 IST, cron, before the Decider). Gemini: Watcher 15 Flash calls/day + 30 Flash-Lite fallback calls/day
  (own counter `watcher:gemini-flash-lite-latest`), max 8 calls per run.
- Decider (today's batch) ~18:00 IST (scheduler `startDecider`) + backup cron 12:53 UTC.
  5 clips per brand every day from Nov 18 (owner's choice, no ramp; optional
  `batch.clips_per_brand_by_date` changes the number from a date on).
Owner: evening Colab render (~19:00+) + YouTube uploads; morning Colab signals (optional).

**Reliable scheduler setup (once, on the laptop, ~10 min):**
1. GitHub > Settings > Developer settings > Fine-grained tokens > Generate: name
   `gta6-scheduler`, repository access "Only select" -> gta6-clipper, permissions:
   **Actions: Read and write** (nothing else). Copy it (never paste it in chat).
2. script.google.com > New project, name "GTA6 scheduler"; replace the code with
   `tools/apps_script_trigger.gs` from the repo.
3. Project Settings (gear) > Script properties > Add: `GH_TOKEN` = the token. Save.
4. Back in the editor, choose `installTriggers` > Run > allow access (Google warns that the
   app is unverified: Advanced > Go to project). Then run `startPipeline` once: the log
   must say `scout.yml: HTTP 204 (started)`.
5. Live-stream mode (optional, same project): paste the new `tools/apps_script_trigger.gs`, add
   Script property `YT_API_KEY` = the YouTube API key (Google Cloud > Credentials; never in chat),
   run `installTriggers` again (adds pollLive every 5 min), then run `pollLive` once: the log says
   "No GTA 6 stream live right now" or counts messages per stream.

**Manual Decider procedure (Claude in chat, only when the owner asks to make or fix a batch by
hand; normally `agents/decider.py` does this automatically):**
1. Read the sheet (Google Drive connector `read_file_content` on the sheet ID): `Moments`
   rows with `source=watcher` and `gate_status=ready`; `Batch` and `Posted` for what's used.
2. Pick the best moments (quality, freshness, Tracker label of the source video, variety
   of content types/creators). Target: one evening batch, up to 5 clips per brand
   (25/day, `settings.batch.clips_per_brand`).
   Use the `Keywords` tab (trend rising/new, updated in the last 12 h) for titles.
3. Assign each to exactly ONE brand, spreading content types across brands.
4. Write per clip: `hook_text` (<= 6 words, works muted), `title_label` (2–5 words, caps),
   `title` (search phrase first, <= 60 chars), `description` (line 1 what happens + "GTA 6",
   line 2 "🎥 Clip from @creator", then the disclaimer from rules.json), `hashtags`
   (#GTA6 #GTA6Shorts #Shorts + 1–2 topic tags), `creator_credit` (@handle or channel name),
   `ig_caption` (Instagram + Facebook: line 1 = keywords + what happens, <= 125 chars;
   line 2 "🎥 @creator"; disclaimer; hashtags). Follow PLAYBOOK.md.
5. Save `batches/<YYYY-MM-DD>.json` (format in `agents/publish_batch.py`), commit on a
   new branch `batch/<YYYY-MM-DD>` from main, push. The Publish batch workflow validates and
   writes the Batch tab; check its log for REJECTED lines and fix them.
6. Clips are auto-approved; tell the owner the batch is in and to run the Colab render.

**Instagram + Facebook auto-post setup (once, on the laptop, ~30-40 min; `agents/post_social.py`):**
1. Each brand's Instagram: Settings > Account type and tools > Switch to professional (Creator or
   Business). Turn OFF "share reels to Facebook" (the API posts to Facebook itself).
2. One Facebook Page per brand (facebook.com/pages/create), linked to that Instagram account
   (Page > Settings > Linked accounts > Instagram).
3. developers.facebook.com > My Apps > Create app (type Business). Add "Instagram" (API setup with
   Facebook login) and "Facebook Login for Business". The app can stay in Development mode (we
   only post to our own accounts).
4. Graph API Explorer: pick the app, "Get User Access Token" with pages_show_list,
   pages_read_engagement, pages_manage_posts, publish_video, instagram_basic,
   instagram_content_publish, business_management; tick all 5 Pages + Instagram accounts.
5. Access Token Debugger: paste the token > "Extend Access Token" (long-lived).
6. Back in Explorer with that token: GET `me/accounts?fields=name,id,access_token,instagram_business_account`.
   Each Page's access_token does not expire.
7. GitHub > Settings > Secrets > Actions > New secret `META_TOKENS_JSON`:
   `{"wasted_again": {"ig_user_id": "<instagram_business_account id>", "page_id": "<Page id>", "token": "<Page access_token>"}, ...}`
   for all 5 brands. Never paste tokens in chat.
8. Google Drive: share the "GTA6 Clipper" folder with the service account email as Viewer.
9. Actions > Social post > Run workflow with "dry run" ticked: the log lists what would post.

**Logging posts:** YouTube posts are found automatically every 6 h (Tracker workflow ->
`log_posts stats` -> discover: our 5 channels' uploads matched to Batch rows by title, so keep
the title from the .txt). Only Instagram links (or a YouTube post whose title was changed a lot)
need pasting. **When the owner pastes post links:** write `posts/<YYYY-MM-DD>.json`
(format in `agents/log_posts.py`; one entry per clip per platform, URLs without tracking
params), commit on branch `posts/<YYYY-MM-DD>` from main, push. The Log posts workflow
fills the Posted tab; the Tracker workflow refreshes YouTube views of our posts every
6 h; checkpoints views_6h / 12h / 24h / 48h / 7d are frozen automatically (interpolated
between 6-hourly readings). Instagram numbers: no free API until the Meta app (PR 6).

## 11. Status / log

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
- 2026-11-15: "Setup sheet" run succeeded after the sheet was shared with
  `gta6-clipper@gta6-clipper-514714.iam.gserviceaccount.com`; all 7 tabs created.
- 2026-11-15 (Chunk A): Scout + Tracker built. Scout: 7 searches/run from
  `settings.youtube.searches` (5 English, "GTA 6 Hindi" with relevanceLanguage=hi,
  finished live streams via eventType=completed) + watchlist uploads; filters non-GTA-6,
  blocklist, < min_views, > 48h; leak titles kept as `blocked_leak`; score =
  views/hour x (1 + log10(1 + outlier capped at 10)) x watchlist/campaign boosts; top 50
  upserted to Candidates (found_at + status preserved). New Candidates columns: score,
  found_by, updated_at; new Tracker column: url. Tracker: snapshots all non-blocked
  candidates, ignores snapshots < 1h apart, labels New/Rising/Breakout/Steady/Fading/
  Mature/Gone, stops at 7 days or 3 slowdowns. Sheet writes are RAW and reads keep strings
  (ids starting with "-" stay text). Clearer "share the sheet" error. 42 tests passing.
  Quota: ~2,900 Scout + ~50 Tracker units/day. Actions use ~8 short runs/day.
  Not yet verified against the live API: owner merges the PR and runs Scout, then Tracker.
  Known: Mature final label (winner/average/low) not computed yet (Analyst/Researcher).
- 2026-11-15: Chunk A verified live: Scout found 291 videos, wrote top 50 (10 campaign
  leads, 711 units); Tracker saved 50 snapshots and committed snapshots.csv.
  Found in real data: GTA 5 streams hashtag-spamming #gta6 ranked #1, Russian titles.
- 2026-11-15 (Chunk B1): Scout now matches GTA 6 in the title text only (hashtags
  stripped; video tags ignored) and keeps only mostly Latin/Devanagari titles
  (`scout.allowed_scripts`). Tracker labels a single-snapshot video `Watching`.
  Signals built: picks top `signals.videos_per_run` live_vod/long candidates (skips
  shorts, Fading/Gone/Mature, done statuses, bad titles); yt-dlp (--skip-download,
  info json + live_chat, 240 s cap, partial files used) -> chat reaction spikes
  (reaction words, last letter may repeat; 10 s bins; >= 3x median and >= 5; window
  starts 30 s before) + Most Replayed peaks (>= 0.6, skip first 5%) ; YouTube
  commentThreads (1 unit) -> timestamp mentions (>= 2 within 10 s). Windows merged
  (15 s gap, max 90 s, x1.5 when 2+ signals agree), top 8 per video -> Moments
  (moment_id = videoid-start, source=signal, gate_status=pending). Candidate status ->
  signals_done / no_signals (only when yt-dlp got video info). Prints YTDLP_BLOCKED if
  YouTube refuses every yt-dlp call from GitHub (then chat/replay move to Colab).
  57 tests. Pending: first live run decides GitHub vs Colab for yt-dlp; then B2
  (Watcher + Decider handoff).
- 2026-11-15: First live Signals run: YouTube blocked yt-dlp on GitHub ("Sign in to confirm
  you're not a bot") for all 6 videos; comment timestamps still gave 5 moments (6 units).
  Owner's Colab test fetched video info fine -> yt-dlp work (chat, replay, Fetcher) runs in
  Colab.
- 2026-11-15 (Chunk B2): Signals split into modes: `api` on GitHub (comments only, status
  -> comments_done) and `full` in `colab/signals.ipynb` (yt-dlp with node JS runtime; owner's
  Google login via google.colab auth in `lib/sheets.get_client`; repo cloned with a read-only
  `GH_TOKEN` Colab secret). Watcher built: jobs = videos with pending signal windows (<= 5
  windows per call via videoMetadata start/endOffset) then long videos <= 20 min whole;
  Gemini returns segment-relative start/end (absolute answers detected), quality 1-10,
  standalone, promo, hook idea; clips fitted to 12-45 s; gate -> ready / rejected_promo /
  rejected_low (< 6) / rejected_context / rejected_used (within 20 s of Batch/Posted) /
  rejected_blocklist. Signal rows -> watched, candidate -> watched. Daily Gemini counter
  `data/gemini_usage.json` (Pacific date), budget 15/day split over 2 runs; models tried in
  order ["gemini-3.8-flash", "gemini-flash-latest"]. New Moments columns: quality,
  standalone, hook_idea, title, channel_title, url, watched_at. Batch handoff:
  `agents/publish_batch.py` + workflow on `batch/**` pushes; validation per clip. 76 tests.
  Pending: owner sets up Colab secrets (GH_TOKEN), first live Watcher run (model name,
  YouTube window support), first batch.
- 2026-11-15: First live Watcher run failed: Gemini didn't answer within 300 s on the first
  video (socket timeout crashed the run; 1 call counted). Fix: timeout -> GeminiError
  (video marked `watch_failed`, run continues), 420 s per call, mediaResolution LOW,
  whole-video limit 15 min, run stops starting new calls after 22 min, unbuffered logs.
- 2026-11-15: Second live Watcher run worked: model `gemini-3.8-flash` accepts YouTube
  windows; 7 calls -> 14 moments (12 ready) in 22 min (~3 min per call, sequential); one
  503 "high demand". Two Spanish videos slipped through (Latin script). Fixes: Gemini calls
  run in parallel waves (`watcher.parallel_calls` = 4); 500/503/timeouts are `Transient`
  -> one retry after 20 s, and the video is retried next run (not watch_failed); usage
  counter thread-safe. Scout/Signals/Watcher drop Spanish/Portuguese titles
  (`scout.foreign_chars`, 2+ `scout.foreign_words`; 'los' allowed for Los Santos) and
  Scout drops videos whose declared language isn't en/hi. 82 tests.
- 2026-11-15 (Chunk C): Fetcher + Renderer built as `agents/render_batch.py` (one Colab run,
  so the downloaded file never has to survive between sessions). Rows with approved=yes
  and render_status != done: yt-dlp `--download-sections *start-end` (<= 1080p, node JS
  runtime) -> faster-whisper word timestamps (model `settings.render.whisper_model`, GPU if
  available, VAD) -> `lib/render.py` .ass + ffmpeg: 1080x1920, blurred darkened background,
  gameplay zoomed 1.25x and centred, top title label (brand accent box), hook text over the
  video for 2.5 s, 3-word uppercase captions under the video with one keyword in the accent
  colour, "via @creator" credit, loudnorm -14 LUFS, x264 veryfast crf 20 -> Drive
  `<output_dir>/<date>/<brand>/<batch_id>.mp4` + `.txt` (title, description, hashtags,
  source link). Batch columns added: output_path, rendered_at, template_version; statuses
  written with one batched update per clip (`sheets.set_values`). 5 brand templates v1
  (accents: Wasted Again FF3B30, Neon Wanted FF2BD6, Mission Failed (now Busted Again) FF9500, Neon Getaway
  00E5FF, Five Star Chaos FFD60A; fonts Anton + Poppins + Noto Sans Devanagari downloaded in
  Colab). Verified locally by rendering a test clip and checking frames. 95 tests.
  Not yet done: gameplay-aware reframing, split-screen facecam (Phase 3).
- 2026-11-15: All 5 YouTube channels created. Handles with dots were taken, so they use
  underscores. "Mission Failed" was rejected by YouTube as a channel name, so that brand is
  now **Busted Again** (key `busted_again`, @bustedagainclips, orange FF9500, fails and
  funny bugs); its Instagram should be renamed to bustedagain.clips.
- 2026-11-16: GitHub delayed or skipped several cron runs overnight (Scout 00:23 skipped,
  Watcher 04:00 never ran; others ran 15 min-4.5 h late). Fix: odd-minute schedules,
  Signals 4x/day, Watcher triggered by `workflow_run` after every Signals run plus 2 backup
  cron slots; `gemini.watcher.runs_per_day` = 4 -> max 4 calls per run so the budget is
  spread over the day (evening batch still gets fresh moments). Owner skipped the Gemini-limits and
  watchlist tasks (no time): keep budget 15/day (429s are handled); watchlist to be filled
  later from recurring Candidates channels.
- 2026-11-16: First test batch (5 clips, one per brand) published to the Batch tab and
  approved. First Colab render: downloads worked, Whisper failed on all 5 with "open() got
  an unexpected keyword argument 'metadata_errors'" (Colab's newer PyAV vs faster-whisper's
  decoder). Fix: `render_batch.load_audio` decodes audio with ffmpeg to 16 kHz float32 and
  passes the array to Whisper; failed rows keep fetch_status=done if the file was fetched.
- 2026-11-16: Second Colab run failed at the clone step: the last cell had `os.chdir`
  into the repo, so re-running "Run all" deleted the working directory before cloning
  (git exit 128), and CalledProcessError printed the token in the URL. Owner told to
  delete and regenerate the token. Notebook fix (both): chdir to /content first, pass the
  token as an http.extraHeader (never in the URL), capture output, clear error message.
- 2026-11-16: The fixed render notebook was overwritten on main by a "Created using Colab"
  commit (owner saved the old open notebook back to GitHub; it also contained run outputs
  with the v2 token, now revoked). Restored the fixed version and added
  `tests/test_notebooks.py` (no outputs, no tokens, safe clone cell) so this can't pass
  review again. Owner: never save notebooks back to GitHub from Colab.
- 2026-11-16: **First clips posted.** Owner rendered the 5 test clips (CPU; Colab GPU quota
  was used up) and posted all 5 on YouTube + Instagram (one per brand). Fixed token
  permissions (fine-grained token needed Contents: Read-only). Posts log built:
  `agents/log_posts.py` (log: posts/*.json -> Posted rows `<batch_id>-<platform>`, moment_id
  from Batch; stats: YouTube views/likes/comments now, views_24h and views_7d frozen by age,
  posted_at from YouTube publish time), `log-posts.yml`, stats step added to the Tracker
  workflow. Posted columns added: platform_id, views_now, likes_now, comments_now, stats_at.
  `posts/2026-11-16.json` holds the first 10 links. 102 tests.
- 2026-11-17 (decisions): reviewed a loophole list (strategy, SEO, editing). Owner chose:
  one batch/day, up to 25 clips (5 per brand); no Sheet approval (auto-approve, glance while
  uploading); YouTube stays manual with YouTube Studio scheduling (API uploads from an
  unaudited project stay private; audit not worth it now); one editing style for all
  brands, derived from what winning Shorts do (Researcher) — only the accent colour differs;
  learn from own Shorts at 6/12/24/48 h checkpoints; Facebook added (Instagram auto-share
  now, Meta API later); Kaggle render later (Colab is fine). Build plan, one PR each:
  1 launch pack, 2 Researcher (full, daily), 3 Gatekeeper + saturation, 4 auto-Decider
  (Gemini), 5 editing pack (from Researcher findings) + Demucs music removal, 6 Instagram +
  Facebook auto-post, 7 Kaggle render test, 8 smart crop + facecam split-screen.
- 2026-11-17 (PR 1, launch pack): Signals (api) re-checks comments of videos < 48 h old every
  6 h (Candidates.signals_at; new windows overlapping known moments are dropped; up to 12
  videos/run; Hindi/extra reaction words). Watcher: clip length per content type
  (`clip_length_seconds`), a too-short clip gets <= 3 s lead-in before the action then the
  rest after (no dead air first); "Gemini busy" retried once (watch_retry) then
  watch_failed; watch_failed videos are skipped for signal jobs too. Hook text moved above
  the title label (templates v2, `hook.y`). Batch: `ig_caption` column (validated, default
  built from title), auto-approve. Scout: keyword miner -> `Keywords` tab (phrase, score,
  videos, trend vs last run). Posted: views_6h/12h/48h added; checkpoints interpolated.
  `sheets.upsert_rows` now keeps columns a record doesn't mention (Scout no longer blanks
  Signals' columns, re-publishing a batch no longer blanks render columns).
- 2026-11-17 (PR 2, Researcher): `agents/researcher.py` + daily workflow (06:53 IST).
  collect: 6 queries x 2 orders (viewCount, relevance), Shorts (videoDuration=short, <= 180 s,
  GTA 6 title) published 3-14 days ago (~1,200 units); score = log10 views + up to 0.5 for
  views/subscribers; labels per day's cohort: winner top 15%, average 30-70th pct, low
  bottom 30%, above = rest -> `Research` tab (new columns channel_title, subscribers,
  published_at, collected_at, score, features, model, credits). analyze: up to 20 winners
  + 20 average not yet tagged, youngest first; Gemini Flash-Lite (models
  ["gemini-3.8-flash-lite", "gemini-flash-lite-latest"], 45/day, own counter
  `data/gemini_usage_researcher.json`) tags BLIND: hook type, text in first second, payoff
  second, title label, caption style/position/colour, layout, facecam, zooms, cuts, slow-mo,
  freeze, replay, shake, SFX (riser/impact/whoosh/scratch/meme/ding), music, voiceover,
  emoji, loop ending, pace, content type, topic, language. findings (last 7 days, >= 8
  per group): winners vs average shares/medians, DO / AVOID when >= 15 points apart; SEO
  (title length, emoji, question, caps, hashtag count, hashtags/phrases winners use more);
  most-clipped creators (@handles in winners' descriptions -> watchlist ideas); today's
  winning topics -> `Playbook` tab rows `research.<section>.<key>`. Claude reads them for
  PLAYBOOK.md + the editing pack (PR 5). Not verified live yet: model name for Flash-Lite,
  whether Flash-Lite accepts YouTube URLs. 122 tests.
- 2026-11-17 (health check after merging PR 1 + 2): Researcher live run OK (433 Shorts found,
  322 GTA 6 Shorts kept: 49 winner / 128 average; `gemini-flash-lite-latest` works with
  YouTube URLs; `gemini-3.8-flash-lite` doesn't exist). Problems found: (1) Watcher produced
  0 moments for 2 days: gemini-3.8-flash answered 503 "high demand" to almost every call,
  each 503 + its retry was counted (15/15 used for nothing), and a per-minute 429 (free
  tier: 5 requests/min for Flash) was treated as "daily budget used". (2) GitHub ran only
  ~half the scheduled jobs in 48 h (Scout 4/8, Tracker 4/8, Signals 2/8), hours late.
  (3) Researcher analysed winners first, so the budget ran out before enough average
  Shorts were tagged (14 vs 5).
- 2026-11-17 (PR 3, reliability): `lib/gemini.py`: a 503 moves the request to the next
  model (Watcher models ["gemini-3.8-flash", "gemini-flash-lite-latest"]); if all are busy,
  wait `retry_wait_seconds` (45) and go through the list once more; 503s, 404s and per-minute
  429s are refunded (not counted); per-minute 429 waits Google's "retry in Xs" (<= 60 s) and
  retries once; client-side pacing `requests_per_minute` (Watcher 5, Researcher 10) shared by
  threads; daily 429 still stops (BudgetReached). Researcher: alternates winner/average,
  budget 100/day, models ["gemini-flash-lite-latest"]. Workflows: chain Scout -> Tracker ->
  Signals -> Watcher via workflow_dispatch from the previous job (GITHUB_TOKEN dispatch is
  allowed to start workflows); cron kept only for Scout (backup), Watcher (12:47 UTC) and
  Researcher; guard jobs; Apps Script scheduler. Actions minutes estimate: ~4 chains/day
  (~8 min) + Watcher (~25 min/day) + Researcher (~12 min/day) ≈ 1,350 min/month; skipped
  guard runs ~1 min each. 130 tests.
- 2026-11-17 (after PR 3 merge): first chain run via the Apps Script scheduler worked end to
  end: Scout -> Tracker -> Signals (12 videos, 4 windows) -> Watcher (4 calls, all answered by
  Flash-Lite because Flash was busy; 8 moments, 7 ready).
- 2026-11-17 (PR 4, Gatekeeper): `agents/gatekeeper.py` + `gatekeeper.yml` (chained after the
  Watcher). Simple gates (leak words `gatekeeper.leak_words` in source title / what happens,
  blocklist by id or name, used within 20 s of Batch/Posted), duplicates (>= 50% overlap of
  the shorter clip -> keep higher quality), saturation: per source video one
  `search_snippets` (part=snippet, videoDuration=short, q "<creator> GTA 6", published after
  the source; 100 units) + stats (1 unit); keep Shorts that reference the source (video id or
  creator name in title/description); cache top 15 in Candidates `sat_shorts` (JSON id/views/
  text) + `sat_checked_at`, refresh after 12 h, max 6 searches/run (budget 700 units/run).
  A moment is saturated if a cached Short shares >= 2 keywords with reason + hook idea + tags
  and has >= 20k views. Gemini client: per-model budgets (`model_budgets`); Watcher Flash-Lite
  fallback 30/day of its own, max 8 calls per run (GitHub minutes). Captions: strong profanity
  / slurs (English + Hindi, `rules.captions.mask_words`, `word*` = prefix) masked in burned-in
  captions ("f******"). Open decision for the owner: make the repo public for unlimited
  Actions minutes (then raise the Watcher budget). 138 tests.
- 2026-11-17 (PR 5, auto-Decider): `agents/decider.py` + `decider.yml` + Apps Script
  `startDecider` (daily 18:00 IST). Pick: ready watcher moments <= 48 h old, score = quality
  + Tracker label bonus (Breakout 1.5, Rising 1, Fading -1...) - 0.03/h after 6 h - 0.5 per
  "N Shorts already show it"; max 2 per video, 3 per creator. Assign: per-brand target from
  the ramp; best clip -> brand with most free slots, preferring brands without that content
  type / creator. Text: one Gemini call (models Flash -> Flash-Lite, own budget 4/day,
  counter `data/gemini_usage_decider.json`) with rising Keywords (last 12 h) + Researcher SEO
  DO-rules; `finish_clip` enforces limits (hook <= 6 words, label 2-5 caps words, title
  starts with GTA 6 and <= 60 chars, base + <= 2 clean topic hashtags, description with
  credit + disclaimer, ig_caption). Re-runs top up today's batch (`start_index` in
  publish_batch). Creator credit = channel name (handles not known yet). Gemini: no
  mediaResolution for text-only calls. Owner must re-paste the Apps Script and run
  installTriggers again to add startDecider. 146 tests.
- 2026-11-19 (Researcher fresh tier): new mode `fresh` (also part of `all`): Shorts 0-72 h old
  from 4 searches (`researcher.fresh.searches`, ~400 units) + Scout's own Shorts (Candidates
  kind=short, free); score = log10(views/hour) + channel-size bonus, labelled within age
  buckets 0-6/6-24/24-48/48-72 h (small buckets merged) -> Research `fresh_label`
  (rising / fresh_average / fresh_low / fresh_above), `views_per_hour`, `fresh_at`; the settled
  `label`/`score` are never touched, and analysed features are reused when the Short settles.
  Up to 10 rising + 10 fresh_average analysed per run. Findings (last 72 h, >= 6 per group,
  20-point difference) -> Playbook `research.now.<section>.<key>`; "SHIFT (7-day: X)" when the
  verdict differs from the 7-day one; `research.now.topics.word:<w>` = topic words rising Shorts
  use much more -> Scout x1.3 (`scout.topic_boost`, found_by "+hot topic"). Gemini tags now
  include `music_type` (phonk, hype_edm, ...; never the song title) and `cuts_on_beat` (for PR 5
  beat-sync). Decider's prompt gets the "now" SEO rules first. Workflow: second cron 11:23 UTC
  runs job `research-fresh` (no guard; named differently so the morning guard is unaffected);
  manual input mode=fresh. Budget: ~1,700 YouTube units/day, up to 80 Flash-Lite calls/day.
  155 tests.
- 2026-11-19 (Colab GPU fix): first render of the 24-clip batch failed on the Colab GPU
  ("libcublas.so.12 is not found": Whisper loads on the GPU, crashes when it transcribes).
  `agents/render_batch.py`: `Transcriber` preloads the pip CUDA libs (`preload_cuda_libs`), and
  if transcribing fails with a GPU/CUDA error it reloads Whisper on the CPU once and carries on
  (`is_gpu_error`); other errors still fail the clip. `colab/render.ipynb` cell 2 installs
  `nvidia-cublas-cu12 nvidia-cudnn-cu12` only when `nvidia-smi` finds a GPU. With a GPU the
  clip downloads, not Whisper, are the slow part; CPU still works (~1-2 min per clip).
  160 tests. Not verified on a real Colab GPU yet: check the first run's log line
  "CUDA libraries preloaded" / "Whisper 'small' on GPU/CPU".
- 2026-11-19 (editing pack): `lib/edits.py` + render_batch. Per clip: loudness analysis finds the
  payoff second (loudest 0.5 s after 1 s) and hits (sudden loudness jumps); plan = punch-in
  zooms (x1.12 for 0.7 s on hits + payoff, max 4, gameplay only so labels/captions stay still),
  impact SFX on the payoff, whoosh before zooms, optional slow-mo replay (payoff -1.2..+0.8 s at
  0.5x appended, second impact inside it), optional end 1.5 s after the payoff (min 8 s).
  SFX generated with ffmpeg lavfi (no files). Second ffmpeg pass only when replay/SFX are on;
  if it fails the plain clip is kept. Which effects: Playbook `research.now.*` > `research.*`
  > `settings.render.edits` defaults (zoom/impact/whoosh on, replay/end-cut/music removal off).
  Music removal: Demucs two-stem (voice kept, rest x0.3), off by default, notebook installs it
  only when on. "Beat sync" = zooms on the clip's own hits (music is added later in the apps).
  `template_version` records the edits (e.g. "2+zoom2,impact,whoosh"). Notebook: `MAX_CLIPS = 3`
  in the last cell for the first test (env CLIPPER_MAX_CLIPS); set 0 after. Checked with real
  ffmpeg on a synthetic clip (payoff/hits found, zoom visible, replay length right). 165 tests.
- 2026-11-19 (notebook fix): render notebook cell 2 crashed on a CPU runtime
  (`FileNotFoundError: nvidia-smi`, my bug in the GPU-library check). Now `shutil.which('nvidia-smi')`;
  test guards it. Lesson: never call GPU tools directly in notebook cells; test the cell logic
  on a machine without a GPU.
- 2026-11-19 (first editing-pack test, 3 clips on Colab CPU): 1 rendered; 2026-11-18-04 failed in
  ffmpeg pass 1 ("... [v] matches no streams / Error initializing complex filters"), -01 failed
  as before (download refused, skipped on purpose). Likely cause: zooms used `scale` with a time
  variable (`eval=frame`, `t`), which Colab's older ffmpeg (4.4) may not support; only clips with
  zooms are affected. Fix: zooms are now an overlay of a constant zoomed copy with
  `enable=between(t,..)` (`edits.zoom_chain`, basic filters only); render_batch retries a clip
  without zooms if ffmpeg still fails, and failed rows keep the start AND end of ffmpeg's error.
  Tested on 3 source shapes + frame check. 166 tests. Lesson: Colab's ffmpeg is older than the
  dev box's: prefer basic filters.
- 2026-11-19 (real cause of clip 04): ffmpeg said "Stream specifier ':v' ... matches no streams":
  the source file had NO video stream (stale/partial/audio-only file kept in the Colab work dir
  from an earlier run; the zoom was never the problem). `probe` now raises if there is no video;
  render_one deletes such a file and downloads again once, else fails with "bad download".
- 2026-11-19 (clip 04 again): even a fresh download of 2026-11-18-04 (video v8z3imeVPCk) has no video
  stream (probably only audio formats offered to yt-dlp for that video; -01 is refused outright).
  yt-dlp FORMAT now requires a video stream in every fallback, and on this failure the run prints
  how many video formats yt-dlp sees + its last message. Owner: skip clips that keep failing.
- 2026-11-23 (YouTube bot check in Colab): render run failed on all 3 clips with "Sign in to confirm
  you're not a bot" (YouTube now blocks Colab's IP too, sometimes). render_batch stops at the first
  bot block without marking anything failed (retried next run) and tells the owner to restart the
  runtime (new IP). Optional cookies: if Drive `GTA6 Clipper/cookies.txt` exists, both notebooks copy
  it and yt-dlp uses `--cookies` (export from a SPARE Google account, never the brand channels'
  account; the file is a login, never commit or share it). Render notebook MAX_CLIPS back to 0.
  167 tests. Clip 2026-11-18-04 had no video stream earlier; -01/-04 should be set approved=no.
- 2026-11-24 (style from winners): owner: no fixed template; every element only if winners use it;
  captions were missing in the Colab clips and text looked boring; hooks were weak. `lib/style.py`:
  the Researcher's findings step writes Playbook `style.*` rows from 7-day winners + rising fresh
  Shorts vs average: title_label / hook_text / caption_keyword_color on if >= 50% of winners use it
  (or >= 30% and 15 points more than average); captions judged only on winners WITH speech (none /
  word_by_word / phrase, position = most common); layout = most common (fill = centre crop, blur,
  black); hook_kind + hook_words + `style.hook_examples` (real winning hook texts). Not enough data
  -> `settings.render.style_defaults`. New Gemini tags: `hook_text_on_screen` (exact text),
  `hook_text_kind` (question/bold_claim/reaction/warning/number_or_list/pov/none). Renderer prints
  "Style (from winners)" with the reasons; captions pop in (scale bounce), one line only (font
  shrinks to fit, WrapStyle 2); hook pops too; label/hook/captions hidden when winners don't use
  them; template_version gets "|<style tag>". Decider prompt: hook must be specific to the clip,
  in the winning hook kind, with winning examples. Debug: each clip saves a 540px frame (middle of
  the first caption) + its .ass to Drive `GTA6 Clipper/_debug/<day>/` (Claude can read these via
  the Drive connector). Captions-missing cause still unknown (local renders show them): check the
  _debug frame of the next run. 174 tests.
- 2026-11-24 (winners = breakouts): owner chose to judge a Short against its OWN channel's normal
  views (option B), not raw views or subscribers. `lib/baselines.py`: per channel, latest 30
  uploads (playlistItems 1 unit + videos 1 unit) -> settled Shorts (>= 2 days old) -> views, cached
  in `data/channel_baselines.json` for 3 days (committed by the Researcher workflow); the Short
  being judged is left out of its channel's median. breakout = views / channel median (fallback
  0.1 x subscribers, else 5,000; `baseline_source` says which). Researcher score (settled and
  fresh, fresh still labelled within age buckets) = log10(1 + min(breakout, 100)) + 0.15 x
  log10(1 + views), -3 under the view floor (10k settled / 1k fresh). Research columns:
  channel_median_views, breakout, baseline_source. Extra quota: own 600 units per run on top of the
  run budget (first day up to ~1,200, then ~100-300/day thanks to the cache). Old rows keep their
  old labels until the daily collect finds them again. 176 tests.
- 2026-11-24 (gap report): `lib/skills.py` SKILLS = every editing trick the Researcher tags, with
  `have` = can the Renderer do it (zoom, impact, whoosh, replay, end cut, label, hook, captions,
  keyword colour, 3 layouts) or not yet (split-screen facecam, shake, freeze, in-clip slow-mo,
  emoji/stickers, loop ending, riser/scratch/meme/ding sounds, fast cuts, cuts on beat, voiceover);
  background music = NOT FOR US (added in the apps). Researcher findings step (same winners as the
  style: 7-day winners + rising fresh vs average) -> Playbook `gaps.<key>` rows ranked:
  BUILD NEXT (can't do, winners >= 15 points more and >= 20% use it), MAYBE, HAVE IT, SKIP.
  Owner reviews a sample of each skill before it ships; when built, flip `have` to True.
  Workflow: manual mode `findings` recomputes Playbook only (style + gaps, no YouTube/Gemini).
  179 tests.
- 2026-11-24 (CI fix): the Tests workflow had failed on every run since the editing pack (Nov 19):
  `ModuleNotFoundError: numpy` (tests/test_edits.py). numpy is installed only in tests.yml
  (`numpy==2.5.3`), NOT in requirements.txt, so Colab keeps its own numpy. Lesson: check the
  Tests run after each merge (`gh run list -w tests.yml`).
- 2026-11-24 (first gap report, live): 86 winners / 149 average (breakout labels): NO missing trick
  is used clearly more by winners (all SKIP). Real differences: full-screen layout (86% of winners;
  72 h: 70% vs 49%), shorter (median 29 s vs 36 s). Style now: fill layout, phrase captions middle,
  hook on, no label, no keyword colour. Owner decided to build skills anyway and TEST them on our own
  posts (build-measure-learn) instead of trusting other creators' data alone.
- 2026-11-24 (skills as experiments): `settings.render.edits.experiments` = {skill: share};
  `edits.experiment_flags` puts each skill on a stable random share of clips (sha1 of batch_id +
  skill), tags in template_version (shake / freeze) so the Analyst can compare our own posts with vs
  without. First skills in pass 2: screen shake (0.4 s jittered crop overlay at the payoff, 18 px,
  basic filters) and freeze frame (payoff frame held 0.6 s with silence; impact on the freeze; later
  whooshes/impacts shifted). Both 50%. lib/skills.py marks them have=True. Sample rendered (test
  pattern) and sent to the owner for review before merge. 181 tests.
- 2026-11-24 (smart crop + split-screen): owner sent a real screen recording (Techno Gamerz bike
  crash) and asked to test in chat; the full-screen centre crop cut off the crash and the facecam.
  `lib/reframe.py`: small grayscale frames via ffmpeg (no new deps); action path = frame-difference
  column centroid every 0.25 s (facecam excluded), median-smoothed, speed-limited (0.3 width/s) ->
  crop x as a flat piecewise-linear ffmpeg expression (no nested if; old-ffmpeg safe). Facecam =
  OpenCV Haar face in the same off-centre spot in >= 50% of 8 sampled frames; webcam box from the
  overlay's sharp edges in the AVERAGE frame (fallback 16:9 ~4 face widths). Verified on the real
  clip (box matched the webcam exactly). Layout `split` (facecam band on top, 25-42% of height,
  gameplay below following the action, hook on the seam, credit/bottom captions inside gameplay)
  is an experiment on 50% of clips WITH a facecam (`settings.render.reframe.split_share`), tag
  `split`; full-screen clips always follow the action (tag `follow`). Any reframe/zoom failure ->
  plain centre-crop retry. Needs OpenCV (preinstalled in Colab; env CLIPPER_HAAR_XML overrides the
  model path). Built on the shake/freeze branch (PR #33). 188 tests.
- 2026-11-24 (Examples tab): owner wants the system to hand him real viral Shorts per editing
  style, which he screen-records and sends to Claude in chat to build skills from. `lib/examples.py`
  + new sheet tab `Examples` (Researcher findings step, every run): "TOP breakouts (any style)"
  (10) + 3 per editing trick in lib/skills.SKILLS, from 7-day winners + rising fresh Shorts, ranked by
  breakout then views; columns url, title, channel, views, breakout, age, have_skill (can we do it
  yet), style (summary of its Gemini tags). Fixed slots `<category>#<rank>` overwritten each run.
  Workflow: owner picks examples -> screen recording to chat -> Claude studies frames -> sample ->
  owner approves -> skill PR (experiment). 183 tests.
- 2026-11-24 (example downloads): `agents/fetch_examples.py` + render notebook cell 6 (`EXAMPLES = 20`,
  0 = skip): after the render, Colab downloads up to 20 Shorts from the Examples tab to Drive
  `GTA6 Clipper/_examples/<date>/NN_<category>_<id>.mp4` + `_list.txt` (category, link, views,
  breakout, style). Order: best example of each trick we can't do yet, then TOP breakouts, then the
  rest; already-downloaded files skipped; stops on YouTube's bot check. For private study only
  (never posted). Owner sends picks to Claude in chat -> skill. 193 tests.
- 2026-11-24 (viral tricks now): owner wants the system to find which editing tricks are viral right
  now and give 10 examples of each. Examples tab reworked: `examples.viral_tricks` ranks every trick
  in lib/skills.SKILLS by lift (share of winning Shorts using it minus share of average Shorts; must
  be used by >= 15% of winners), on the last 72 h (rising vs fresh_average, if >= 6 each) else 7-day
  winners + rising vs average; top 5 tricks x 10 examples (best breakouts first), slots
  `trick<k>#<n>`, plus `top#<n>` (10 breakouts of any style). New columns trick_rank, trick_use.
  Researcher log prints "Viral tricks right now". Colab cell 6 `EXAMPLES = 30` = top 3 tricks x 10,
  files `NN_trick<k>-<name>_<id>.mp4`. Settings `researcher.examples`. 194 tests.
- 2026-11-24 (experiment report): owner: judge experiments within a day, not a week. `lib/experiments.py`
  + `log_posts.experiment_report` (runs in the Tracker workflow's stats step, every ~6 h): for each
  skill under test (`render.edits.experiments` keys + split/follow when reframe is on) compares our
  YouTube posts WITH vs WITHOUT it (tags from Batch.template_version) at the earliest checkpoint
  where both sides have >= 3 posts: views_24h, else 12h, else 6h; medians (an outlier can't
  decide); BETTER/WORSE at >= 1.3x either way, "LOOKS ..." + "(early, few posts)" until >= 10 per
  side. -> Playbook `experiments.<skill>` rows + log. Needs posted links logged (posts/*.json);
  Instagram has no stats yet. 197 tests.
- 2026-11-24 (auto-log YouTube posts): `log_posts discover` (runs inside `stats`, i.e. every
  Tracker run, and in `all`): each brand's handle -> channel id (channels forHandle) -> latest 50
  uploads -> matched to that brand's unlogged Batch rows by normalised title (difflib >= 0.75,
  hashtags/emoji ignored, not before the batch day) -> Posted rows `<batch_id>-youtube`. ~15
  units/run. Owner no longer pastes YouTube links; Instagram still by hand until the Meta API.
- 2026-11-24 (clip length from winners): Researcher findings write Playbook `style.clip_max_seconds`
  = 75th percentile length of 7-day winners + rising fresh Shorts (needs >= 8, clamped 15-60 s;
  from Research `length_s`, no Gemini needed). Watcher caps every content type's clip length at it
  (`style.apply_length`, min = max - 5 when needed) and tells Gemini the shorter lengths. Moments
  found before the cap keep their length. 198 tests.
- 2026-11-24 (@handles): Scout saves the channel's @handle (channels.list snippet.customUrl, no
  extra quota) in new Candidates column `channel_handle`. Decider credits `@handle` when known
  (YouTube links it): description "🎥 Clip from @handle (Channel Name)", ig_caption
  "🎥 Channel Name (@handle on YouTube)" (no bare @ on Instagram: it would tag a random IG account);
  renderer overlay "via @handle". Falls back to the channel name.
- 2026-11-24 (loudness signals): Signals full mode (Colab `colab/signals.ipynb`) now also finds
  LOUD moments when a video has no chat replay (normal long videos, or streams without chat):
  smallest audio-only stream via yt-dlp -> ffmpeg 4 kHz mono -> dB per second (numpy) -> seconds
  >= `loudness_spike_db` (8) above the median of the minute around them (first 5% skipped),
  grouped within 5 s, top 10 -> windows 20 s before to 10 s after, signal `loud`. Merged with
  comments/Most Replayed like the others (2 signals agreeing x1.5). Audio file deleted after.
  Tested on synthetic audio with two bursts. rules.signals.loudness_*.
- 2026-11-24 (live-stream mode): Scout gets an 8th search `eventType=live` (streams live now ->
  Candidates kind=live_now). Apps Script `pollLive` (every 5 min, free, owner's Google account):
  top 3 live_now candidates by score -> videos.list (activeLiveChatId) -> liveChatMessages.list
  (5 units, up to 3 pages of 2,000 per poll, page token kept in Script Properties) -> messages and
  reactions (same word list as rules.json, a test keeps them equal) per 10 s since actualStartTime
  -> `LiveChat` tab (one row per video+bin, added up). Live-mode quota cap 2,500 units/day
  (Script Properties counter). When the chat ends: Candidates kind -> live_vod and Signals is
  dispatched (if GH_TOKEN is set) -> Watcher -> Gatekeeper, so a stream's best moments are
  Moments minutes after it ends, without Colab. Signals (api and full) use LiveChat counts as the
  chat signal when there's no yt-dlp chat (`live_chat_counts` -> `spikes_from_counts`, same spike
  rule) and handle those videos first; loudness is skipped for them. Tested pollLive in node with
  fake Google services. Built on the loudness PR (#41). 197 tests.
  Note: YouTube's quota page now lists search.list with its own bucket of 100 calls/day (1 unit
  each); Scout (8 x 4 runs) + Researcher + Gatekeeper stay under it, but don't add many searches.
- 2026-11-24 (Instagram + Facebook auto-post): `agents/post_social.py` + `social.yml` (dispatched by
  the Tracker every ~6 h, + manual with dry-run). Posts Batch rows with render_status=done,
  approved != no, rendered >= `social.veto_hours` (2) ago and <= 48 h, max 2 per brand per run
  (spreads ~5/day). MP4 read from Drive by the service account (`<batch_id>.mp4`; owner shares the
  folder), uploaded by resumable upload (rupload.facebook.com; no public link): Instagram Reel
  (container -> upload -> wait FINISHED -> media_publish -> permalink) and Facebook Page Reel
  (video_reels start/upload/finish). Caption = ig_caption. Batch columns ig_status, ig_url,
  fb_status, fb_url, social_at; Posted rows `<batch_id>-instagram/-facebook`. Token per brand in
  secret META_TOKENS_JSON (Page tokens, no expiry; never printed, masked in errors). Does nothing
  until the secret exists. A failed post retries next run; a published post whose link lookup
  fails is still marked done (no double posts). Setup steps in section 10. 203 tests.
- 2026-11-25 (render newest first): ~88 approved clips from 20-25 Nov were never rendered, and the
  renderer took rows oldest first, so a render would spend its time on stale moments. `to_render` now
  sorts newest batch day first and skips batches older than `render.max_batch_age_days` (1 = today +
  yesterday); older unrendered rows simply stay unrendered. 212 tests.
- 2026-11-25 (skills on every clip): owner decided: once a skill is built it goes on ALL clips it fits
  (no 50/50 split); he judges skills from our own analytics over time + other creators' clips
  (Researcher). `render.edits.experiments` shake/freeze = 1.0, `reframe.split_share` = 1.0 (every
  facecam clip gets split-screen). New skills: add them at 1.0. The experiment report now in effect
  compares clips with a skill vs older clips without it (before/after), so read it with that in mind.
  The "auto shares" idea (verdicts setting 10-90%) was dropped before merge. Owner will start posting
  again once the current viral editing skills are built; old unrendered batches are skipped (#44).
- 2026-11-26 (Signals crash fix): the live-mode PR made Signals fail on GitHub from its first run
  (2026-11-25 19:06 and 21:32 UTC): inside main's loop `vid, live = ..., kind == "live_vod"`
  overwrote the LiveChat dict, then `live.get(vid)` -> AttributeError. Renamed to `is_vod`. New
  end-to-end test runs `signals.main()` in api mode on fake sheets (LiveChat tab included) and
  checks a chat moment is written; it fails on the old code. Lesson: test main(), not only helpers.
- 2026-11-26 (health check): all workflows green after #46; Signals verified (live-chat windows from
  the LiveChat tab: 601 messages -> 3 windows). Live mode works (LiveChat: 2 streams, 763 bins).
  Auto-log found 11 of our YouTube Shorts (best: 1,180 and 917 views). Fix: `style.clip_max_seconds`
  used the winners' 75th percentile = 52 s, above every content type's own limit, so it capped
  nothing; now the winners' MEDIAN (29 s today). Supply note: only 10 clips in the 25 Nov batch
  (target 25) because few ready moments; should rise now that Signals runs again.
