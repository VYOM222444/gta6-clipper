# GTA6 Clipper Playbook (v0.1 — hand-written starter)

This is the starting rulebook every agent reads. It is based on general short-form
practice, not yet on our own data. The Researcher replaces these rules with measured ones
as data comes in. Every rule that changes gets a date and the reason.

Location in repo: `playbook/PLAYBOOK.md` (machine-readable values go in `playbook/rules.json`).

## 1. What makes a moment worth clipping

- Stands on its own: makes sense with no context from the original video.
- The payoff (funny, shocking, impressive) happens early, ideally within the first 3 seconds
  of the clip, never later than ~10 seconds.
- Clear emotion: laughter, shock, rage, hype or "wow".
- Strong signals: a chat spike in the stream's chat replay, a loudness spike, a Most
  Replayed peak, many timestamped comments, or a fast-rising source video.
- Not already clipped many times (saturation check). Big-streamer moments (e.g. the biggest
  reaction streams) get clipped by everyone; prefer fast-rising mid-size creators and less-
  clipped moments.

Skip: slow setups, sponsor reads, promo segments, moments that need explaining, leaked footage.

## 2. Finding the moment inside a long stream (untested)

- Chat spike: count reaction messages ("LMAO", "💀", "CLIP IT", "NO WAY", "W", "😂") per
  10-second window; a window far above the stream's normal level marks a moment.
- Chat reacts ~20 seconds after the moment: search from ~30 s before the spike to the spike,
  then trim so the payoff still lands in the first 3 seconds.
- Loudness spike (screams, hype) is the fallback when there is no chat replay.

## 3. Length

- Chaos / reaction: 15–30 seconds.
- News / secret / tip: 20–40 seconds.
- Cut dead time. If nothing new happens for ~2 seconds, cut or speed it up.

## 4. Hook (first 1–3 seconds)

- Start mid-action. Never start with a slow intro.
- On-screen hook text in the first second, max ~6 words.
- Hook types to rotate and test: shock moment first; question ("Did he really…?");
  bold claim ("GTA 6 cops are insane"); headline ("GTA 6 just…"); reaction face first.
- Make it work with sound off.

## 5. Pacing and editing

- Something changes every 2–3 seconds: a cut, zoom, caption change or sound effect.
- Punch-in zooms on the key moment; brief freeze or slow-mo only on the payoff.
- Loop where possible: the ending flows back into the start to encourage rewatches.

## 6. Captions

- Word-by-word or 2–4 word chunks, synced to speech.
- Large, bold, high contrast, inside the safe zone (away from the bottom ~20% and the right
  side where the app's buttons sit).
- Highlight 1–2 keywords per line in the brand's accent color (yellow is the common
  default in GTA clips); emoji sparingly.

## 7. Framing

- Vertical 9:16.
- Top title label: a small boxed label at the top with 2–5 words naming the moment
  ("KFC IN GTA 6?"). It stays on screen for the whole clip, separate from the hook text.
- Gameplay: follow the action, not a fixed center crop.
- Streamer clips: split-screen (gameplay top, facecam bottom).
- Fallback: blurred background behind the full frame.

## 8. Packaging (SEO starter rules — to be replaced by Researcher findings)

- Title: search phrase first, then the hook. e.g. "GTA 6 police chase goes wrong 😂".
  Keep under ~60 characters.
- Description line 1: what happens + "GTA 6". Line 2: credit "🎥 Clip from @creator".
  Then 3–5 hashtags: #GTA6 #GTA6Shorts #Shorts + 1–2 topic tags.
- Every brand description includes: "Fan channel. Not affiliated with Rockstar Games."
- Campaign clips: follow the campaign's own rules (required tags, links, paid-partnership
  setting).

## 9. Timing thresholds (Tracker / Scout starting values)

- Act on Shorts and long videos 6–24 hours old.
- `Rising`: velocity accelerating (≥1.5× the previous snapshot's velocity).
- `Breakout`: Rising AND views ≥ 3× the channel's subscriber count (or ≥ 3× its median).
- `Fading`: velocity dropping for 2 snapshots in a row.
- Final label (winner / average / low) only at 7 days, compared against GTA Shorts of the
  same age.
These numbers are guesses. Tune them after ~2 weeks of real data.

## 10. Posting

- Each clip goes to one brand only; that brand posts it on YouTube + Instagram (+ Facebook).
- Add a trending sound inside the app when the clip has no important speech.
- Review every clip before posting.
- Spread posts through the day rather than all at once.

## 11. Safety

- Prefer creators who allow clipping (clipping campaigns count); always credit them;
  transform every clip (also protects against YouTube's "reused content" rule).
- No leaked footage, no watermark removal, no duplicate clips across brands.

## Change log

- 2026-11-14: v0 created by Claude (hand-written starter rules).
- 2026-11-15: v0.1. Added chat-spike and loudness signals (new section 2), top title label
  (section 7), yellow keyword highlight default, saturation note on big-streamer moments,
  campaign-clip packaging rule. Reason: 5 reference reels about GTA 6 clipping tools
  (PeakCut, a chat-spike clip finder, OpenShorts promos) all used this format and these
  signals. Not tested on our own data yet.
