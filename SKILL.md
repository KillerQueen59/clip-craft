---
name: clip-craft
description: Expert system for turning long-form video into short vertical clips — picking which moments are clip-worthy, setting exact in/out points, burning captions, and auto-reframing widescreen to 9x16. Also designs and builds the pipeline that does this at scale. Crucially, this skill learns over time by keeping a per-project ledger of every clip proposed versus what the user actually shipped, then distilling that into house rules that make later runs better. Use this skill whenever the user mentions clipping, clip generation, shorts, Reels, TikTok cuts, podcast highlights, finding the best moments in a video, auto-reframe, burned-in captions, opusclip-style tools, or wants to build or improve a video-clipping pipeline — even if they only describe the symptom, like clips feeling as though they start too late or not knowing how to pick timestamps. Also use it after a clipping session to record feedback so the ledger improves.
---

# Clip Craft

Cutting video is trivial. Deciding *what* to cut, *exactly where* to start it, and *where the frame should look* is the entire job. This skill treats the mechanical parts as solved plumbing and spends its effort on judgment — then captures that judgment so it compounds.

## The one idea that matters

A clipping system has three layers, and they fail in different ways:

| Layer | Fails as | Fixed by |
|---|---|---|
| Transport (decode, cut, mux) | Crashes, A/V drift | Correct ffmpeg invocation |
| Perception (ASR, faces, speakers) | Wrong words, jittery crop | Better models, smoothing |
| **Judgment (what's worth clipping, where it starts)** | **Clips that are technically fine and nobody watches** | **Feedback loop — this skill** |

Almost everyone builds layer 1 and 2 and then wonders why the output is mediocre. Put your effort in layer 3.

## Workflow

Follow this order. Skipping to "run the model" produces clips that need heavy manual fixing.

### 1. Load the ledger first

Before proposing anything, check for a project ledger at `.clipcraft/` in the working directory (or wherever the user keeps it).

```bash
python scripts/ledger.py status --dir .clipcraft
```

If it exists, read `.clipcraft/style.md`, `.clipcraft/hooks.md`, and `.clipcraft/calibration.json`. These are learned house rules for *this specific creator* and they override the generic defaults in this skill. If no ledger exists, offer to initialize one — this is what makes the system improve over time:

```bash
python scripts/ledger.py init --dir .clipcraft
```

### 2. Get word-level transcript

Segment boundaries live between words, not between whisper's 30-second chunks. Word timestamps are non-negotiable. See `references/pipeline.md` for the exact commands.

### 3. Propose candidate segments

Read `references/segment-selection.md` and score candidates against the rubric there. Return them as structured data, always with a reason:

```json
[{"start": 412.30, "end": 461.85, "hook": "...", "why": "...", "score": 8.2, "risks": ["mid-story reference to earlier context"]}]
```

Over-propose. It's cheaper for the user to reject 6 of 10 than to miss the good one.

### 4. Snap the boundaries

The LLM's timestamps are a rough guess and will be 1–3 seconds off in ways that feel wrong. Always post-process: snap to word edges, add lead-in/tail padding from `calibration.json`, verify no mid-sentence cut. `references/segment-selection.md` has the snapping algorithm.

### 5. Render

Cut, caption, reframe. `scripts/cut_clip.py` handles the deterministic parts. Reframing strategy is in `references/pipeline.md`.

**Every rendered clip gets editable transcript sidecars.** Alongside each `clip_NN_<fmt>.mp4`,
`run` writes (clip-local time, `0` = clip start): `clip_NN.srt` (canonical caption grid),
`clip_NN.txt` (plain text), and `clip_NN.words.json` (per-word karaoke timing — array of
`{start, end, word}`). To fix a bad caption, edit **either** the `.srt` **or** the `.words.json`
and run `recut --clip N`; recut re-renders using whichever file was touched most recently (SRT wins
ties) and shifts the clip-local times back to source internally. This is the same sidecar set
`--raw`/`recut` emit, so `run`, `--raw`, and `recut` are interchangeable edit entry points.
Engine: `cli.py` → `_write_transcript_sidecars` + `_write_clip_words`.

When the transcript itself is *wrong* (not just a typo to hand-edit) — echoed vocab, a
"Subtitle by …" hallucination over music, or a dropped span on code-switched speech — re-decode
just that clip's audio prompt-free with `recut --clip N --retranscribe` (`transcribe.py →
transcribe_span`). Re-decoding a short window with no `initial_prompt`/`hotwords` and VAD on
removes the whole-file bias that makes the long pass echo/hallucinate. See the "Transcript format"
rules in the project `CLAUDE.md` for the root cause (Words.txt→hotwords) and the corrections.txt
post-pass.

For sources where two things matter at once — two people talking, or people reacting to on-screen content (an app demo, a chart, gameplay) — a single crop throws away the point. Use a **stacked split-screen layout** (triptych / duo / streamer) instead. These can be always-on (`fit = "stack"`) or auto-switched with the plain face crop when the content appears (`fit = "triptych"`). See `docs/stack-layouts.md` for the presets, config, the working trading-source example, and the honest "when it works vs. needs manual regions / a better face model" guidance.

### 6. Record what happened — this is the step people skip

After the user reviews the clips, capture the delta between what was proposed and what was shipped. Trim adjustments, rejections, and reasons are the training signal.

```bash
python scripts/ledger.py record --dir .clipcraft --session session-id.json
```

Ask for feedback in a form that's cheap to give: "Which of these would you actually post, and for the ones you'd fix — did the start come too early or too late?" Don't ask for essays.

### 7. Distill periodically

Every ~10 recorded sessions (or when the user asks), run a distillation pass. Read `references/learning-loop.md` for how to turn the raw ledger into updated `style.md`, `hooks.md`, and `calibration.json`. This is the actual mechanism by which the skill gets better — the skill file stays fixed, the ledger grows.

```bash
python scripts/ledger.py stats --dir .clipcraft   # shows whether it's time to distill
```

## Reference files

Read these as needed rather than all at once:

- **`references/segment-selection.md`** — the scoring rubric, hook taxonomy, boundary-snapping algorithm, and the failure modes of LLM-picked timestamps. Read this for any "which moments should I clip" question.
- **`references/pipeline.md`** — concrete ffmpeg/ASR/reframe recipes, including the crop-path smoothing that separates good auto-reframe from bad. Read for implementation.
- **`references/learning-loop.md`** — ledger schema, what to record, and the distillation procedure. Read when initializing a ledger or when it's time to distill.
- **`references/architecture.md`** — system design, recommended tech stack, and a phased build roadmap with realistic timelines. Read when the user is building the system rather than using it.
- **`docs/stack-layouts.md`** — stacked split-screen layouts (triptych face/content/face, duo face/face, streamer face/gameplay): the panel model, `[triptych]`/`[stack]` config, banner-based content switching, the A-vs-B call for multi-cam podcasts, and when fixed regions are reliable vs. when the footage needs a better face model. Read when a source shows two people at once or people reacting to on-screen content.

## Working principles

**Prefer over-proposing to under-proposing.** Recall beats precision at the candidate stage; the user is the filter and they're fast at it.

**Never trust an LLM timestamp directly.** Always snap to the word grid. This single step fixes most "the clip feels wrong" complaints.

**A clip that needs context isn't a clip.** If understanding the moment requires something said four minutes earlier, it fails regardless of how good the moment is. Flag these in `risks` rather than silently shipping them.

**Reframe stability beats reframe accuracy.** A crop that's slightly off-center but rock steady looks professional. A perfectly centered crop that jitters looks broken. When in doubt, hold the shot.

**Record the negative examples.** Rejected clips teach more than accepted ones. The ledger's `rejections` are the highest-value data in it.

**Be honest about the ceiling.** Automated selection gets to "good enough that a human editor picks from a shortlist" quickly, and to "ship without review" slowly or never, depending on content type. Interview-style content with clear Q&A structure automates well. Rambling or visually-driven content does not. Say so rather than promising more.
