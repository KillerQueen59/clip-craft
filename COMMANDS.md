# ClipCraft — Command Cheat Sheet

Everything you can run **without Claude**. Run all commands from
`D:\MeSelf\Clip System`. Python launcher is `py -3`.

> Full reference lives in `README.md`. This file is the quick lookup.

---

## 0. Before anything — start Ollama & check the box

Selection needs a **local Ollama** running. It does not start itself.

```powershell
# Start the Ollama server (leave this window open, or it runs as a tray app)
ollama serve

# One-time: pull the model this project uses (see [select] model in config.toml)
ollama pull qwen2.5:7b

# List what you already have
ollama list
```

Then verify the whole environment (ffmpeg, whisper, Ollama+model, GPU, edge-tts):

```powershell
py -3 -m clipcraft doctor
```

`doctor` prints `OK / WARN / FAIL` per check. Fix every **FAIL** before running.
If it says _"ollama not reachable"_ → you forgot `ollama serve`.
If it says _"'qwen2.5:7b' not pulled"_ → run the `ollama pull` above.

---

## 1. The five commands you'll actually use

| Command      | What it does                                                       |
| ------------ | ------------------------------------------------------------------ |
| `init`       | Scaffold a new project folder (config + brief + source/ + output/) |
| `candidates` | List clip moments only — **no rendering** (fast preview)           |
| `run`        | Pick moments **and** render the top clips                          |
| `recut`      | Re-render one clip after fixing its caption words                  |
| `doctor`     | Environment check                                                  |

Distribution (optional): `upload` (YouTube), `tiktok` (drafts), `telegram` (to phone), `core` (montage).

### Typical workflow

```powershell
# 1. Make a project once
py -3 -m clipcraft init "Webinar Crypto Narrative 2027"
#    -> edit projects\Suli Crypto Live\config.toml and brief.txt
#    -> drop the video into projects\Suli Crypto Live\source\

# 2. Preview candidates (no render) to sanity-check the picks
py -3 -m clipcraft candidates --project "Suli Crypto Live" --source "video.mp4" --n 30

# 3a. Render fresh (re-picks candidates via the model, then renders)
py -3 -m clipcraft run --project "Suli Crypto Live" --source "video.mp4" --n 40 --render-top 20

# 3b. Render the EXACT candidates you previewed in step 2 (no re-selection)
py -3 -m clipcraft run --project "Suli Crypto Live" --source "video.mp4" --from-candidates --render-top 20
```

> `run` normally re-runs the model, so its picks can differ from the `candidates`
> table you reviewed. Use `--from-candidates` to render straight from the existing
> `output\<name>\candidates.json` instead — it skips selection, honors the file's
> order, and respects any hand-edits (e.g. fixing titles). `--n` is ignored;
> `--render-top` still controls how many render. Same `--name`/source ⇒ same job
> folder, so it finds the file you just wrote. The transcript is reused from cache
> either way (whisper never re-runs).

Without a project folder, point at a file/URL directly:

```powershell
py -3 -m clipcraft run --source "https://youtu.be/XXXX" --n 10 --render-top 3
py -3 -m clipcraft run --source "D:\path\video.mp4" --name ep1 --formats mobile,desktop
```

---

## 2. Answers to your specific questions

### "Only candidates" (no rendering)

```powershell
py -3 -m clipcraft candidates --project "X" --source "video.mp4" --n 30
```

Writes `output\<name>\candidates.json` and prints a scored table. Nothing is rendered.

### "How many clips"

Two separate knobs:

| Knob           | Controls                              | Where                          |
| -------------- | ------------------------------------- | ------------------------------ |
| `--n`          | How many _ideas_ the model proposes   | flag, or `[select] candidates` |
| `--render-top` | How many of them become _video files_ | flag, or `[clip] render_top`   |

```powershell
py -3 -m clipcraft run --source "video.mp4" --n 40 --render-top 20
```

Over-propose (`--n` high), render fewer (`--render-top`), you pick the winners.
Realistic max candidates ≈ `transcript_chunks × [select] max_per_chunk`.

### "Config to reframe" — how the video fills the vertical frame

Set `[clip] fit` in `config.toml` (or the project's config):

| `fit`      | Result                                                                      |
| ---------- | --------------------------------------------------------------------------- |
| `reframe`  | **Face-tracking close-up** — follows the speaker, fills the frame (default) |
| `crop`     | Fills the frame, crops the sides (single locked talking head)               |
| `contain`  | Whole frame + black bars (nothing lost)                                     |
| `blur`     | Whole frame, bars filled with a blurred copy (best for charts/screens)      |
| `triptych` | Auto-switch face ↔ content banner ↔ face (reaction-to-screen)               |
| `stack`    | Always-on duo (face/face) or streamer (facecam/gameplay)                    |

Use `contain`/`blur` for screen recordings and charts so nothing is cropped off.
`triptych`/`stack` need `[triptych]`/`[stack]` regions per source — see `docs/stack-layouts.md`.

### "Focus frame only" — lock onto the talking person

`fit = "reframe"` is the focus-frame mode. It has three sub-modes via `[reframe] mode`:

```toml
[clip]
fit = "reframe"

[reframe]
# mode unset  -> plain face tracking (default): follows the on-screen face,
#                picks the mouth that's moving in a two-shot
# mode = "asd"     -> active-speaker detection: for N-person multicam, picks WHICH
#                     face is actually talking (clipcraft/asd.py)
# mode = "speaker" -> diarization-anchored talker pick (needs [diarize] enabled = true)
sample_fps = 6.0       # higher = finer tracking, slower
switch_dwell = 1.0     # min seconds locked on one face (anti-flicker)
switch_margin = 1.4    # how much a speaker must out-move the other to steal focus
```

Plain `reframe` is right for most talking-head / interview / podcast footage.
Use `mode = "asd"` when several people sit in separate camera boxes and you need
the crop to jump to whoever is speaking.

### "Detect laugh" — promote moments the room laughed at

Audience-laughter detection is `[reaction]` (PANNs audio model). On by default:

```toml
[reaction]
enabled = true         # detect laughter/cheer and re-rank candidates
require = false        # NEVER true for comedy — many funny clips have no on-mic laugh
threshold = 0.20       # probability counted as "laughter present"
reaction_boost = 2.5   # score points added, scaled by how hard they laughed
snap_out = true        # land the OUT point ON the punchline laugh
out_lookahead = 4.0    # seconds past the model's end to hunt for the laugh
laugh_tail = 0.6       # end this long after the laugh burst finishes
```

It only ever **promotes** clips with laughter and snaps the ending onto the laugh —
it never drops a clip for being silent. Keep `require = false`.

### "Hook TTS" — AI voice reads the hook over the opening

`[tts]` — free, local `edge-tts` (needs internet at render time). On by default:

```toml
[tts]
enabled = true
engine  = "edge-tts"
voice   = "id-ID-GadisNeural"   # female; id-ID-ArdiNeural = male
source  = "hook"               # hook | title — which text is read aloud
rate    = "+0%"                # "+10%" faster, "-10%" slower
gain    = 1.0                  # voiceover volume multiplier
hold_first_frame = true        # freeze opening frame while the voice reads, then roll
blur_sigma = 24                # blur on the frozen frame (0 = none)
duck    = 0.35                 # (only when hold_first_frame=false) original audio level under the voice
```

The separate visual/cold-open hook is `[hook]` (`mode = "clip"` cold open,
`"text"` teaser, or `"off"`). `[tts] source` just chooses which text the voice reads.

Install once if `doctor` warns it's missing:

```powershell
py -3 -m pip install edge-tts
```

---

## 3. Fix a bad clip without re-running everything

```powershell
# 1. Emit an editable per-clip transcript
py -3 -m clipcraft recut --project "X" --clip 3
#    -> output\<run>\clips\clip_03.words.json

# 2. Fix the `word` fields (keep the start/end timings), save.

# 3. Re-run the SAME command to render just that clip
py -3 -m clipcraft recut --project "X" --clip 3
```

Uses the cached transcript — never re-runs whisper, other clips untouched.

---

## 3b. Thumbnails (9:16 title-card)

**`run` makes these automatically** — every rendered clip gets a
`clip_NN_<fmt>.thumb.jpg` next to its `.mp4` (recorded in the manifest under
`thumbnails`). Turn it off with `[thumbnail] title_card = false` in config.

To (re)generate without re-rendering — after a `recut`, or for clips rendered
before this feature existed:

```powershell
py -3 -m clipcraft thumbs --project "X"                       # all clips
py -3 -m clipcraft thumbs --project "X" --clips 1,3 --format mobile
```

The thumbnail is the **clearest face frame** from the source (sharp — no
captions, no TTS-freeze blur) with the clip's **yellow title card** overlaid at
the same spot it's burned on the video. Reuses the existing
`clip_NN_<fmt>.title.png`; renders the card fresh from the manifest title if it's
missing. Auto-detects the run folder and source video (pass `--name` / `--source`
if ambiguous).

---

## 4. Publishing (optional)

```powershell
# YouTube Shorts — see README for one-time OAuth setup
py -3 -m clipcraft upload   --project "X" --name video --privacy unlisted
py -3 -m clipcraft upload   --project "X" --name video --clips 1-10 --dry-run

# TikTok drafts (two-step auth first: --auth then --code)
py -3 -m clipcraft tiktok   --project "X"

# Send to your phone via Telegram (for manual Reels)
py -3 -m clipcraft telegram --project "X" --whoami   # get chat_id first
py -3 -m clipcraft telegram --project "X"
```

`--clips 1-10` or `--clips 1,3,5` limits which clips; `--force` re-does already-sent ones.

---

## 5. Where things land

```
projects/<name>/
├─ config.toml            # your overrides on top of the root config.toml
├─ brief.txt              # do/don't guidance (auto-applied, no --brief needed)
├─ source/                # drop videos here (use bare filename with --source)
└─ output/<run>/
   ├─ candidates.json     # every scored candidate
   └─ clips/
      ├─ clip_01_mobile.mp4
      ├─ clip_01_thumb.jpg
      └─ manifest.json     # titles, timings, upload results
```

Global defaults: root `config.toml`. Per-project: `projects/<name>/config.toml`
(only list what differs — it's merged on top of the root).

Example

py -3 -m clipcraft run --project "Daging Ini/PERANG ISLAM - PUTBAL" --source "YTDown.com_YouTube_PERANG-ISLAM-PUTBAL_Media_MEyWVJqUPfs_001_1080p.mp4" --n 10 --render-top 5

py -3 -m clipcraft upload --project "Pojok Jenaka\Vior Langsung Tes Vocal Bareng Idgitaf, Jegel & Mario Cuman Bisa Bantu Doa Aja" --name "Vior Langsung Tes Vocal Bareng Idgitaf, Jegel & Mario Cuman Bisa Bantu Doa Aja" --clips 7 --account main --privacy unlisted

py -3 -m clipcraft tiktok --project "Pojok Jenaka\Apa Kabar Yono, Pandu, Rais, Yudha" --name "YTDown.com_YouTube_Apa-Kabar-Yono-Pandu-Rais-Yudha_Media_ohJ" --clips 6 --account main --privacy unlisted

py -3 -m clipcraft tiktok --project "Pojok Jenaka\Vior Langsung Tes Vocal Bareng Idgitaf, Jegel & Mario Cuman Bisa Bantu Doa Aja" --name "Vior Langsung Tes Vocal Bareng Idgitaf, Jegel & Mario Cuman Bisa Bantu Doa Aja" --clips
