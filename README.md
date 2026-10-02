# ClipCraft

Local-first video clipping. Submit a video (URL or file), get vertical **and**
desktop clips with styled burned-in captions. Selection runs on your local
Ollama; transcription on faster-whisper (GPU if available). Nothing leaves your
machine — no API keys, no per-clip cost.

TOKEN : set your HuggingFace read token in env HF_TOKEN (see config.toml [diarize])

## Setup (once)

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

This installs ffmpeg, the Python packages, ensures the `llama3.1:8b` Ollama
model is present, and runs the environment check.

Verify anytime:

```powershell
py -3 -m clipcraft doctor
```

## Use

```powershell
# List candidate moments only (fast, no rendering)
py -3 -m clipcraft candidates --source "https://youtu.be/XXXX" --n 10

# Pick candidates AND render the top clips
py -3 -m clipcraft run --source "https://youtu.be/XXXX" --n 10 --render-top 3
py -3 -m clipcraft run --source "D:\path\to\video.mp4" --name "ep1" --formats mobile,desktop
py -3 -m clipcraft run --source "D:\path\to\video.mp4" --n 100 --render-top 20 --name "ep1" --formats mobile,desktop

# Steer selection with a do/don't brief (file path or inline text)
py -3 -m clipcraft run --source "D:\path\to\video.mp4" --name "ep1" --brief "brief.txt"
py -3 -m clipcraft candidates --source "https://youtu.be/XXXX" --n 10 --brief "only clips about the villain; no intros or ads"
 cd "D:\MeSelf\Clip System"; py -3 -m clipcraft upload --name "ep1" --privacy unlisted
```

### Command options

| Flag           | Applies to | Meaning                                                                           |
| -------------- | ---------- | --------------------------------------------------------------------------------- |
| `--source`     | both       | video URL (yt-dlp) or local file path                                             |
| `--n`          | both       | how many candidate moments to propose (shortlist size)                            |
| `--render-top` | run        | how many top candidates to render (default from config)                           |
| `--name`       | both       | output folder name under `work/` (default: derived from the source)               |
| `--formats`    | run        | comma-separated formats to render, e.g. `mobile,desktop` (default from config)    |
| `--brief`      | both       | a do/don't brief — a file path or inline text — that constrains what gets clipped |
| `--config`     | all        | path to a project config that overrides `config.toml` (see Per-project configs)   |

`--n` controls how many ideas the LLM returns; `--render-top` controls how many
become video files. Over-propose and let yourself be the filter.

### Steering selection with a brief

A brief is explicit do/don't guidance injected into the selection prompt, so the
model only proposes clips you actually want. Pass a file or inline text:

```powershell
py -3 -m clipcraft run --source "..." --brief "brief.txt"
py -3 -m clipcraft run --source "..." --brief "only villain moments; skip intros and ads"
```

`brief.txt` is plain text — see `brief.example.txt`:

```
DO: emotional moments, strong opinions, surprising facts, funny lines
DON'T: intros and greetings, sponsor or ad reads, technical tangents, politics
TOPIC FOCUS: only clips about movie villains, plot twists, or acting
TONE: punchy, curiosity-driven, a little provocative
```

To apply a brief to every run without the flag, set `[select] brief_file` in `config.toml`.

## Projects (recommended workflow)

A project is one self-contained folder per channel/brand under `projects/`.
It holds its own config, brief, source videos, and output — nothing scattered.

```powershell
py -3 -m clipcraft init "Suli Crypto Live"
```

creates:

```
projects/Suli Crypto Live/
├─ config.toml     # branding & settings (overrides base config.toml)
├─ brief.txt       # do/don't guidance (auto-applied, no --brief needed)
├─ source/         # drop your video files here
└─ output/         # clips, thumbnails, candidates, manifest land here
```

Then edit `config.toml` + `brief.txt`, put a video in `source/`, and run with
`--project` (source is a **bare filename** resolved from `source/`):

```powershell
py -3 -m clipcraft run    --project "Suli Crypto Live" --source "video.mp4" --n 40 --render-top 20
py -3 -m clipcraft upload --project "Suli Crypto Live" --name "video" --privacy unlisted
```

`--project` loads that folder's config, auto-applies its `brief.txt`, finds the
source in `source/`, and writes everything to `output/`. Each run gets its own
subfolder `output/<name>/` (name defaults to the source filename):

```
output/<name>/
├─ candidates.json          # every scored candidate: hook, description, score, risks
└─ clips/
   ├─ clip_01_mobile.mp4     # 1080x1920 (9:16)
   ├─ clip_01_thumb.jpg      # auto thumbnail (clear-face frame + title)
   ├─ clip_02_mobile.mp4  …
   └─ manifest.json          # titles, descriptions, source seconds, thumbnail paths
```

Transcripts are cached (keyed by content hash), so re-running the same video
skips transcription. (`--config <file>` is still available as a lightweight
alternative to a full project folder.)

## Clip length

Length is **content-driven**, not fixed. Each clip covers **one complete topic** —
from where it's introduced to where it fully resolves — so it never cuts off while
the same point is still being made. Clips run as long as the topic needs (up to the
`max_seconds` ceiling, default 90s, still well under the 180s Shorts limit).
`min_seconds`/`max_seconds` are only guardrails: a too-short fragment is grown by
whole following sentences (never mid-sentence), and a too-long one is trimmed back
to a sentence boundary.

## Hook (cold open)

`[hook] mode` controls the opening hook:

- `clip` (default) — **cold open**: the single most gripping moment of the clip
  (the model marks it as `hook_start`/`hook_end`) is copied to the very front, then
  the full clip plays in order. The moment plays twice, and the output runs
  `hook length + clip length` (e.g. a 40s clip with a 4s hook → 44s). Length bounds:
  `clip_min_seconds` / `clip_max_seconds`. Strongest hook — use this by default.
- `text` — burns a short **text teaser** (the model's `hook` field, a 3–7 word
  curiosity punch) over the first `duration_seconds` on a highlight box.
- `off` — no hook.

A cold open is only added when the hook sits in the **back** of the clip, so
pulling it to the front actually rewards staying. `[hook] relocate_after` (default
`0.5`, the midpoint) is the split: a hook in the front half plays **raw** with no
cold open; a hook in the back half is copied to the start. When the model marks a
valid `hook_start`/`hook_end` it's used (grown to `clip_min_seconds` if tiny);
otherwise the anchor falls back to a sentence near the clip's midpoint — which
lands in the front half, so unmarked clips default to raw.

## Fitting the frame (mobile)

`[clip] fit` controls how the source is placed in the vertical frame:

- `crop` — fills the screen, cropping the sides (good for a single talking head)
- `contain` — shows the whole frame with black bars (nothing lost)
- `blur` — shows the whole frame, filling the bars with a blurred copy — best for
  screen recordings / charts so no content is cut off

## Thumbnails

`[thumbnail] enabled = true` generates a `clip_XX_thumb.jpg` per clip: it samples
frames across the clip, uses face detection to pick the one where the person is
clearest (not a chart frame), and burns the title over it (1280×720 by default).
Needs `opencv-python<5` (4.x has the Haar face API; 5.x removed it).

## Fixing a bad clip (recut)

When one clip has bad caption text (a mis-transcription, or a stray language), you
don't re-run the whole pipeline — you edit just that clip's words and re-render it:

```powershell
# 1. Generate an editable, per-clip transcript (one file, just this clip's words):
py -3 -m clipcraft recut --project "Suli Crypto Live" --clip 3
#    -> writes output\<run>\clips\clip_03.words.json

# 2. Open clip_03.words.json, fix the `word` fields, KEEP the start/end timings.

# 3. Re-run the SAME command — now the file exists, so it renders:
py -3 -m clipcraft recut --project "Suli Crypto Live" --clip 3
```

The recut reuses the cached transcript and never re-runs whisper, so your other
clips are untouched. It re-applies the current hook rule and captions from your
edited words, and regenerates the thumbnail. `--clip N` is the number from
`manifest.json`; add `--formats mobile,desktop` to control outputs, `--name` if a
project has more than one run folder.

## No Chinese / CJK

Chinese occasionally leaked in from two places; both are now closed:

- **Captions (ASR):** the Suli projects pin `[transcribe] language = "id"`, so
  whisper never misdetects a stretch as Chinese. English jargon still transcribes.
  If a stray CJK word survives, `render` warns which clip to `recut`.
- **Titles / hooks (Qwen):** the selection model is told the language and forbidden
  from CJK, and any CJK it returns anyway is stripped deterministically before it
  can reach a title card or thumbnail.

## Publishing to YouTube Shorts (bulk)

A clip is treated as a Short automatically when it is vertical (9:16) and under
3 minutes — which the `mobile` format already is. `clipcraft upload` reads a
run's `manifest.json` and uploads each mobile clip via the YouTube Data API
(1 quota unit per upload, ~100 uploads/day as of 2026).

**One-time setup (needs your Google account):**

1. [Google Cloud Console](https://console.cloud.google.com) → create a project
2. Enable **YouTube Data API v3**
3. Create an **OAuth client ID** → application type **Desktop app** → download it
4. Save it as `client_secret.json` in the project root (`D:\MeSelf\Clip System\`)
5. On the OAuth consent screen, add your Google account as a **Test user**
6. Install the upload extras:
   ```powershell
   py -3 -m pip install google-api-python-client google-auth-oauthlib
   ```

**Upload a run:**

```powershell
# See exactly what would upload — no auth, no network
py -3 -m clipcraft upload --name "ep1" --dry-run

# Upload the mobile clips (opens a browser once to authorize the first time)
py -3 -m clipcraft upload --name "ep1" --privacy unlisted
py -3 -m clipcraft upload --name "ep1" --privacy public --every 90
```

| Flag        | Meaning                                                |
| ----------- | ------------------------------------------------------ |
| `--name`    | the run folder under `work/` to upload                 |
| `--format`  | which rendered format to upload (default `mobile`)     |
| `--privacy` | `private` / `unlisted` / `public` (default `unlisted`) |
| `--every`   | seconds between uploads, anti-spam (default `60`)      |
| `--dry-run` | list what would upload without calling YouTube         |

Each upload's video ID and URL are written back into `manifest.json`. Title comes
from the clip's `title` (falls back to its hook); description is built from the
clip reason plus the `[upload] hashtags` in `config.toml`.

> Notes: an unverified OAuth app in "testing" mode issues tokens that expire
> after ~7 days — re-authorize weekly or publish the app. Start with
> `--privacy unlisted` to review a batch before making it public.
> TikTok bulk auto-posting is intentionally not included: TikTok's Content
> Posting API requires an app audit for public posts, so it's a manual/draft
> flow rather than a one-command upload.

## Per-project configs

`config.toml` holds your global defaults. For a specific channel/brand, make a
small override file and pass it with `--config` — it is **merged on top of**
`config.toml`, so you only list what differs. See `projects/suli.toml`:

```toml
# projects/suli.toml — inherits everything from config.toml, overrides the branding
[title]
prefix = "Live Take Profit Show Suli :"

[watermark]
text = "@suliantoindriaputra"

[outro]
enabled = true
text = "Nonton selengkapnya di YouTube: @suliantoindriaputra"

[select]
brief_file = "work/brief_crypto.txt"

[upload]
title_prefix = "Live Take Profit Show Suli :"
hashtags = ["#takeprofitshow", "#suli", "#crypto", "#Shorts"]
description_footer = "Nonton selengkapnya di YouTube: @suliantoindriaputra"
```

Use it on any command:

```powershell
py -3 -m clipcraft run    --config projects\suli.toml --source "..." --name suli-ep1
py -3 -m clipcraft upload --config projects\suli.toml --name suli-ep1 --privacy unlisted
```

Anything not in the project file (caption color, formats, model, …) is inherited
from `config.toml`. Make one file per channel: `projects\suli.toml`,
`projects\ojan.toml`, etc.

## Configuration

Everything user-tunable is in `config.toml` — no code editing required.

**Transcription**

- `[transcribe] model` — `small` (fast) → `large-v3` (best, esp. Indonesian)
- `[transcribe] device` — `auto` uses your NVIDIA GPU when present
- `[transcribe] language` — `auto`, or force e.g. `id` / `en`

**Selection**

- `[select] model` — the Ollama model that picks moments (`llama3.1:8b`)
- `[select] brief_file` — optional do/don't brief applied to every run
- `[select] max_per_chunk` — candidates requested per model call (default 15).
  A long video is split into a few transcript chunks, so the realistic total
  is `chunks × max_per_chunk` (e.g. a 30-min video ≈ 3 chunks ≈ ~45). Asking for
  `--n 100` won't hang — it just returns as many as the model finds.
- `[select] request_timeout` / `num_ctx` / `max_output_tokens` — Ollama call tuning

**Upload** `[upload]` — YouTube Shorts publishing

- `client_secret` — path to the OAuth desktop client (default `client_secret.json`)
- `category_id`, `hashtags`, `description_footer` — applied to each uploaded video

**Clips**

- `[clip] min_seconds / max_seconds` — length guardrails (floor / ceiling)
- `[clip] lead_in / tail` — padding around snapped boundaries
- `[clip] render_top` — how many clips `run` renders by default

**Output formats** — add/remove sizes here

- `[output] render` — which formats to render, e.g. `["mobile", "desktop"]`
- `[formats.mobile] / [formats.desktop] / …` — width/height per format
  (add your own, e.g. a `square` 1080×1080)

**Title card** `[title]` — a generated headline shown on the clip

- `enabled`, `prefix` (fixed text before the headline, e.g. a show name),
  `text_color`, `box_color`, `box_opacity`, `font`, `font_size`
- `position` (`top` / `bottom`), `margin`, `side_margin` (wrap width),
  `duration_seconds` (0 = whole clip), `uppercase`, `bold`
- The headline text is written per-clip by the selection model (the `title` field);
  the on-screen title is `prefix + headline`.

**Outro card** `[outro]` — a closing call-to-action burned into the final seconds

- `enabled`, `text` (e.g. `Nonton selengkapnya di YouTube: @handle`)
- `duration_seconds` (how long before the end it appears), `position`
  (`top` / `center` / `bottom`), `text_color`, `box_color`, `box_opacity`,
  `font`, `font_size`, `uppercase`, `bold`
- `hide_video` (default `true`) — during the outro the main video is hidden so the
  CTA is the sole focus: with `fit = "blur"` only the blurred background remains;
  other fit modes fade to black
- `fade_audio` (default `true`) — the audio fades out across the outro window

**Watermark** `[watermark]` — your handle, shown for the whole clip

- `enabled`, `text` (e.g. `@ojantigakali`), `color`, `opacity`
- `position` (`center` / `top` / `bottom`), `font`, `font_size`, `bold`

**Captions** `[captions]` — styling for the burned-in transcript

By default captions render **karaoke-style**: 2-3 words on screen at a time in
`text_color` (white), with the word _currently being spoken_ flipped to
`highlight_color` (yellow) — the active-word look every viral Short uses. The
whole line stays put; only the highlight moves, word by word, in sync with the
audio.

- `highlight` (default `true`), `highlight_color` — the active-word colour
- `text_color` — the base (non-active) word colour
- `style` (`outline` or `box`), `outline_color`, `outline_width`, `shadow`,
  `box_color`, `box_opacity`
- `font` (default `Arial Black`), `font_size` (auto-scales per format),
  `uppercase`, `bold`
- `bottom_margin` — caption baseline as a fraction of height; `~0.40` sits in
  the lower-centre band (viral placement), `~0.15` pins it near the bottom
- `words_per_line` — words per caption; `2-3` is the viral norm, `1` for a
  strict one-word-at-a-time cadence

Common looks:

| Want                                         | Set                                                                              |
| -------------------------------------------- | -------------------------------------------------------------------------------- |
| Karaoke viral style (default)                | `highlight=true`, `text_color="#FFFFFF"`, `highlight_color="#FFE100"`            |
| Flat single colour (no highlight)            | `highlight=false`, `text_color="#FFD400"`                                        |
| Subtitle bar                                 | `style="box"`, `text_color="#FFFFFF"`, `highlight=false`                         |
| One word at a time on a white box (image #3) | `style="box"`, `text_color="#111111"`, `box_color="#FFFFFF"`, `words_per_line=1` |
| Sentence case                                | `uppercase = false`                                                              |

Title, watermark, and caption changes apply to both formats and every future run.

> Note: boxes are rectangular — libass (the subtitle engine) can't draw rounded
> corners or the gradient border seen in some templates. Text, color, fill, and
> position all match; only the corner radius differs.

## How it works

1. **ingest** — yt-dlp for URLs, passthrough for local files
2. **transcribe** — faster-whisper with word-level timestamps (cached, GPU if available)
3. **select** — local LLM proposes scored candidate moments (complete thoughts)
4. **snap** — boundaries corrected to word/sentence edges + padding
5. **render** — ffmpeg cut → cover-crop to each format → burn styled captions → loudness-normalize

The judgment layer (step 3) determines whether clips are good. It over-proposes
on purpose; you are the filter.

## Roadmap

See `CLIP-SYSTEM-PLAN.md` (build plan) and `CLIP-LEARNING-PLAN.md` (how the
system will learn _where_ to clip from your feedback over time). The learning
loop — ledger, calibration, retrieval of past winners — is designed but not yet
built; today every run is stateless.
