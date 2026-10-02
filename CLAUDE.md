# Clip System — House Rules

Rules below apply to **every project** under `d:\MeSelf\Clip System`. They are enforced in the
pipeline code (`clipcraft/`) and must survive across sessions and new projects.

## Token-efficient clipping (ALWAYS ENFORCE)

Selection runs on **local Ollama, not Claude** — the only thing that spends Claude credits is what
Claude reads. Keep sessions cheap:

1. **NEVER `Read` a source transcript** (`output/transcripts/*.json`, 500 KB+ ≈ 200K tokens in one
   call). Nothing needs it. To see wording, read a clip's `.srt` (2–3 KB) or `Grep` the transcript.
   Never full-`Read` anything > ~50 KB — use `Grep` or `Read` with `offset`/`limit`.
2. **Review a run with `clipcraft review [--clips 1,3]`** — it prints scores, times, titles, hooks,
   and the spoken caption text (from the small `.srt`) as a few KB. Use it to judge cuts and author
   titles instead of opening `candidates.json` / `manifest.json` / `words.json` per clip.
3. **Edit the `.srt`, never the `.words.json`** (700 vs ~6K tokens; recut prefers `.srt` anyway).
   Batch all edits, then do a single `recut` sweep — don't round-trip per clip.
4. **Run the heavy steps in a terminal, not through Claude.** Do `clipcraft run` / `candidates` /
   transcribe yourself (or via the `!` prefix); bring Claude in only for review → titles → caption
   fixes → recut.
5. **Delegate bulk reading to an Explore subagent** so large content never enters the main context.

## Title rule (ALWAYS ENFORCE)

Every clip **title** must be interesting **and carry full context** — it must make sense to
someone who saw none of the source video. A title must contain:

1. **WHO** — the specific person/subject, using proper names from the transcript. Never vague
   pronouns ("dia", "this guy", "he/she").
2. **WHAT** — the concrete claim, number, or action.
3. **SOURCE / relationship** — where the claim comes from, when it adds stakes.

**Gold-standard example:**
> `PUTRI TANJUNG SPILL GAJINYA PER BULAN DARI CHAIRUL TANJUNG`
> — names the who (Putri Tanjung), the specific what (spill monthly salary), and from whom
> (Chairul Tanjung).

**Reject** vague teasers that hide who/what, e.g. "This Changed Everything", "You Won't Believe
This". Language always matches the transcript (Indonesian by default).

Enforced in: `clipcraft/select.py` → `_build_prompt()` (the `title` field spec). The same title is
burned onto the clip as the yellow title card via `clipcraft/render.py`.

## Stacked split-screen layouts (triptych / duo / streamer)

When a source shows two things at once — two people talking, or people reacting to on-screen
content (app demo, chart, gameplay) — a single crop throws away the point. Use a stacked layout:
`fit = "triptych"` (face/content/face, auto-switched by content detection) or `fit = "stack"`
(always-on duo face/face, or streamer facecam/gameplay). Engine: `clipcraft/triptych.py`;
integrated in `clipcraft/render.py`. Regions are per-source (SOURCE pixels), set in `[triptych]` /
`[stack]` in the project `config.toml`. Full model, config, working example, and the limits
(fixed regions only reliable on stable composites / locked angles; live multi-cam needs a better
face model + active-speaker detection) → **`docs/stack-layouts.md`**.

**Duosplit manual override — force single crop for part of the clip.** `fit = "duosplit"` auto-decides
split (top/bottom, both talkers) vs. single (active-speaker crop) per segment. To *stop* the split
and hold a single frame over part of a clip, add per-clip fields to that clip's **manifest** entry
(clip-local seconds; the single crop still auto-follows the active speaker):

```json
{ "clip": 4,
  "duosplit_single_after": 42.0,          // >= 42.0s: always single crop
  "duosplit_single_windows": [[10.0, 15.5]] } // only inside these spans: single crop
```

Cuts land exactly on the given seconds; segments outside the windows keep auto split/single. Either
field is optional. After editing the manifest, re-render with `recut --clip N`. Engine:
`clipcraft/duosplit.py` → `_force_single_intervals` / `_apply_single_override`; wired via
`render.py` passing the `clip` entry into `build_base`.

**Manual reaction split (`stack_windows`) — force a duo/trio split over ONE moment.** When a
group-reaction beat (everyone laughing, a collective "pecah ketawa") lands on a **locked wide/master
shot**, a single reframe crop wastes it — you want every face at once. Overlay a configured `[stack]`
layout on top of the normal reframe base for just that window, via a per-clip **manifest** field
(clip-local *content* seconds; the freeze-intro offset is applied automatically):

```json
{ "clip": 4, "stack_windows": [[48.3, 54.8]] }
```

Regions come from `[stack].panels` in the project `config.toml` (SOURCE pixels, one panel per band,
top→bottom). Example trio (left-pair / center / right-pair) from a 1920×1080 locked master:

```toml
[[stack.panels]]
rect = { x = 250,  y = 270, w = 620, h = 367 }   # top band  — left pair
[[stack.panels]]
rect = { x = 700,  y = 290, w = 470, h = 278 }   # mid band  — center person
[[stack.panels]]
rect = { x = 1170, y = 270, w = 620, h = 367 }   # bottom band — right pair
```

Panel `rect` aspect should match the output band (~1.69 for a 1080-wide third of 1920 tall) to avoid
distortion; `mode = "cover"` (default) crops to fill. **Only reliable on a LOCKED camera angle** —
constrain the window to the exact sub-span the master shot is on screen (it will crop the wrong thing
during a 2-shot / close-up). Outside the window the clip stays normal reframe. After editing the
manifest, re-render with `recut --clip N`. Verified working on the Yono clip04 group-laugh
(source 78–85s). Engine: `render.py` (`manual_windows` → `triptych.overlay_tail`); **not** supported
on a clip that has a relocated cold-open hook (`hook_seg`).

## Reframe focus: never blank, never lag (ALWAYS ENFORCE)

In every active-speaker reframe (`fit = "reframe"`, `[reframe].mode = "asd"`) the crop must
always hold a **person's face** and switch to a talker **immediately** — no drift onto empty
space, no easing through the void before it catches up. Two invariants make this true; both are
in code (not config toggles) and must stay:

1. **A stall holds on a person, not the dead center.** A silent pause is NOT an overlap. Silence
   inherits the adjacent single speaker (holds on whoever last spoke; a silent lead-in back-fills
   from the next speaker), so a pause keeps that person's face framed instead of collapsing to the
   blank midpoint between two seats. Enforced in `clipcraft/diarize.py` → `_raw_spans` (overlap vs.
   silence are tracked apart; only genuine overlap stays `duo`).
2. **The crop always contains a real face; corrections are instant.** Seats are a clip-wide
   median, so on switched multicam (or a duo midpoint) the target can land where nobody is on the
   current camera. If no detected face falls inside the crop, snap to the nearest real face —
   immediately (`force_snap`), never eased. A speaker change also snaps, so focus never lags the
   voice. Enforced in `clipcraft/asd.py` → `compute_asd_crop_path` (`faces_cx_at` + the
   `0.85 * halfw` in-frame check). No-op when the right speaker is already framed, so clean shots
   don't regress.

The honest limit: on a wide two-shot where both genuinely talk at once, invariant 2 snaps to one
face rather than a real split — the proper fix there is a stacked layout (above), not plain asd.
Verified working on `Daging Ini / COACH JUSTIN` clip_04 (single-cam hand-off) and clip_05
(near-identical seats). Do not remove these to "smooth" or "simplify" the crop path.

## Always keep raw cuts (ALWAYS ENFORCE)

**Every project must always have a `raw_cuts/` full-frame cut of exactly what shipped** — a
1920×1080, un-reframed, un-captioned cut of each clip's *content span* (`start`→`end`, the same
≤90s / ≤60s span the clip covers, WITHOUT the title/TTS intro). This is what a later manual-focus
pass and `finish` reads back. It is not optional and not tied to `--raw` mode.

Enforced in code: a normal `clipcraft run` (reframe render) **also** writes the raw cut next to the
finished clip, and `recut` keeps it in sync with a retimed span. Engine:
`clipcraft/cli.py` → `_write_raw_cut()` (shared by `run`, `recut`, and the `--raw`/`_export_raw`
manual-focus path); wired in `cmd_run` and `cmd_recut`. Toggle per project with
`[clip] keep_raw_cuts = false`; base `config.toml` defaults it **on**.

When hand-finalizing a run (dropping/retiming clips, re-selecting), make sure `raw_cuts/` ends up
matching the final manifest: clear stale span-named cuts and regenerate for the kept clips (reuse
`_write_raw_cut` per manifest entry — no GPU, just an ffmpeg cut). A dropped clip must not leave a
stale raw cut behind.

## Always separate content into three deliverables (ALWAYS ENFORCE)

**Every project must always ship the same three video deliverables per clip, in separate folders.**
Never merge them, never skip one — this is automatic, not something the user has to ask for.

1. **Raw cut** → `<project>/raw_cuts/clip_NN_raw_<span>.mp4` — full-frame 1920×1080, **un-reframed,
   un-captioned**, content span only (no title/TTS intro, no outro). Engine: `_write_raw_cut()`.
2. **Main Content** → `<project>/main_content/clip_NN_<fmt>.mp4` — the finished **9:16 reframe** of
   the same span but **RAW**: no burned captions, no TTS hook, no title card, no outro. This is the
   editable reframe base a manual pass builds on. The editable transcript sidecars
   (`clip_NN.srt/.txt/.words.json`) are copied in beside it. Engine: `render.py`
   → `_write_main_content()`, driven by the `main_out` arg of `render_clip()`; it reuses the crop
   path already computed for the final render (no second face-detection — one extra encode).
3. **Final Content** → `<project>/output/<job>/clips/clip_NN_<fmt>.mp4` — Main Content **plus** the
   TTS hook, burned captions (transcript), title card, and outro. This is the shipped `_mobile.mp4`.

So per clip: Raw cut ⊂ Main Content (adds reframe) ⊂ Final Content (adds hook + captions + outro).
The **transcript** (`.srt`/`.words.json`) is a component of Final Content (the burned captions) and
also travels as an editable sidecar in `main_content/` (and `clips/`).

Enforced in code: `run` and `recut` both write all three; `recut` keeps `raw_cuts/` and
`main_content/` in sync with a retimed/edited span. Wired in `clipcraft/cli.py` → `cmd_run` /
`cmd_recut` (`main_out` + sidecar copy). Toggle per project with `[clip] keep_main_content = false`
(and `keep_raw_cuts = false`); base `config.toml` defaults both **on**. When hand-finalizing a run,
make `main_content/` match the final manifest just like `raw_cuts/` — no stale file from a dropped
or retimed clip.

**Note on split/composite fits:** Main Content is the clip's *base framing*. For `fit = "duosplit"`
it is the composited duo/single base; for `triptych`/`stack` it is the underlying reframe/crop base
(the phone/panel overlays are part of Final Content only).

## Clip selection: no overlap, no intro (ALWAYS ENFORCE)

Two hard constraints on which spans become clips:

1. **Clips must not overlap each other.** No two shipped clips may cover the same source
   time range, even partially. Each clip is a distinct, non-overlapping span — never two cuts
   of the same moment. When candidates collide, keep the stronger one and drop/retime the other.
2. **A clip must not contain the source's intro.** Skip the opening — channel bumper, title
   sequence, "halo semuanya, selamat datang di...", sponsor/ad read, recap montage. The first
   words of a clip must be real content, not the show's boilerplate open. Set `start` past the
   intro.

**Overlap (constraint 1) is now enforced in code.** `clipcraft/select.py` → `resolve_overlaps()`
runs as the final selection step (`cli.py` → `_pipeline_to_candidates`, and defensively on the
`--from-candidates` load path). It is greedy by score: keeps the stronger clip on every collision,
trims a mere padding-kiss overlap (≤1.5s) so neighbours abut, and drops a weaker clip that re-cuts
the same moment. The returned set is guaranteed pairwise non-overlapping. Snapping runs first because
lead/tail padding can push adjacent clips into each other — so the guard runs last.

**Intro (constraint 2) is still a selection-stage judgment rule** — not enforced in code. Apply it
when proposing/snapping candidates and when hand-editing `candidates.json` / the manifest; verify
before render. (The brief template already hints "DON'T: intros, ads" at `cli.py`.)

## Clip duration: 90s hard ceiling (ALWAYS ENFORCE)

**No clip's content span may exceed 90 seconds.** This is a hard cap on the spoken-content span
(the `start`→`end` of the cut, including lead/tail padding). It is **not** measured on the final
rendered file — a TTS/freeze title intro prepended to the front is allowed to push the shipped MP4
slightly past 90s. The cap governs the *content*, not the intro.

**Enforced in code, and the ceiling wins even over a per-project `max_seconds` override:**
- `clipcraft/snap.py` → `snap()`: `max_s = min(config max_seconds, 90)`; long thoughts trim back to
  the last sentence end that fits, and a final clamp guarantees `end - start ≤ 90` *after* lead/tail
  padding is applied.
- `clipcraft/select.py` → `_sanitize()`: the sanitize ceiling `hi` is likewise `min(config, 90)`.
- Base `config.toml` `[clip] max_seconds = 90`. A project may set a **lower** `max_seconds`, but a
  higher value is silently clamped to 90 by the code above — so the rule survives new projects.

When hand-editing `candidates.json` / the manifest, keep every `end - start ≤ 90`; verify before
render.

## Transcript format (ALWAYS ENFORCE)

**SRT is the default, canonical editable transcript format** for reviewing and recutting clips.
When a clip's captions need fixing, edit the `.srt` — not the `.words.json`.

- The pipeline emits editable transcript sidecars for **every** clip: `run` writes
  `clip_NN.srt` + `clip_NN.txt` + `clip_NN.words.json` next to each `clip_NN_<fmt>.mp4`
  (`cli.py` → `_write_transcript_sidecars` + `_write_clip_words`); `--raw` and `recut` write the
  same set. All use **clip-local time** (0 = clip start). `.words.json` is an array of
  `{start, end, word}` (per-word karaoke timing); `.srt` is the caption grid. Edit either, then
  `recut --clip N`.
- `recut` reads whichever of `clip_NN.srt` / `clip_NN.words.json` was touched most recently, and
  **SRT wins ties** (`cli.py` → `cmd_recut`, `st_mtime >=`). Editing the `.srt` therefore always
  takes over automatically — no need to delete the json.
- `clip_NN.words.json` is **not** a competing default — it is the per-word timing source for
  karaoke-style caption highlighting. Keep it; don't remove it.
- **Bad transcript on a clip (echoed vocab / silence hallucination / dropped code-switched
  speech)?** Don't hand-guess the missing words — re-decode that clip's audio prompt-free with
  `recut --clip N --retranscribe` (`clipcraft/transcribe.py` → `transcribe_span`: no
  `initial_prompt`, no `hotwords`, VAD on, `condition_on_previous_text=False`). It overwrites the
  clip's `.srt`/`.words.json` from the fresh decode, then renders. **Root cause of those echoes:**
  a project `Words.txt` used to also seed whisper `hotwords` (whole-file bias) which whisper echoed
  back on silence ("Sun Go Kong Sun Go Kong"). `config.py _apply_words_file` now seeds
  `initial_prompt` ONLY — set `[transcribe] hotwords` explicitly if a project truly needs it.
  Systematic mis-hearings (e.g. `Surmili`→`Suruh Milih`) go in `corrections.txt` (post-decode
  find/replace, survives re-decode).
- **Gotcha:** `recut` reads `clip_NN.srt`, **NOT** `clip_NN.hq_review.srt`. The `.hq_review.srt`
  is a read-only review copy the pipeline never consumes — edits there are silently ignored.
- Fix caption **text** only; leave the sequence numbers and `HH:MM:SS,mmm --> ...` timing lines
  intact. Titles are a separate field (manifest / title rule above), not in the SRT.

## Text-to-voice (hook voiceover)

- Engine: **edge-tts** (free, local, no API key). Indonesian voices: `id-ID-GadisNeural` (f),
  `id-ID-ArdiNeural` (m).
- Purpose: read the cold-open **hook** aloud over the first seconds of each clip.
- Config lives under `[tts]` in `config.toml`; per-project `config.toml` can override voice/enabled.
