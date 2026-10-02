# Stacked split-screen layouts (triptych · duo · streamer)

Project-local reference for the stacked-layout engine. The code is
`clipcraft/triptych.py` (engine) + `clipcraft/render.py` (integration); this file
is the *why* and the *how to configure*. It travels with the repo so it survives
new sessions and applies to every project under `d:\MeSelf\Clip System`.

A whole family of vertical layouts is one idea: an **ordered stack of panels**,
where each panel crops a region of the 16:9 source into a full-width band. Instead
of throwing away everything outside one crop (plain reframe), you keep two or three
things on screen at once, stacked top-to-bottom.

| Preset | Panels (top→bottom) | Use when | Footage we have |
|---|---|---|---|
| **triptych** | face · content · face | two people reacting to on-screen content (app demo, chart, photo) already composited between them in the source | ✅ Trading source — proven |
| **duo** | face · face | two people talking at once — show both instead of cutting | ⚠️ PKA podcast — multi-cam, hard (see limits) |
| **streamer** | face · gameplay | a gaming/desktop stream: facecam on top, game below | ❌ no raw 16:9 stream yet, only vertical reference clips in `training/` |

All three run through the same engine (`clipcraft/triptych.py` → `stack_subgraph`)
and the same render integration (captions, title card, TTS hook, watermark, outro
all layer **on top** of the stack).

## Panel types

- **cover** — crop the region to the band's aspect and fill it. Use for **faces**
  (a face should fill its band; some head-crop is fine, like a facecam).
- **contain** — fit the whole region inside the band, letterbox filled with a
  blurred zoom of itself. Use for **content** (a phone screen, a game, a chart) so
  nothing important is cropped off.

## Two ways to trigger a stack

1. **Always on** — `fit = "stack"`. The stack is the whole clip. This is duo and
   streamer, where the layout never changes.
2. **Content switch** — `fit = "triptych"`. The base is normal face-reframe, and
   the stack is overlaid **only while the content is on screen**, detected per
   frame (the trading source is detected by its green sponsor banner). Everywhere
   else the clip is a normal face crop. This is the "change focus between face and
   content" behaviour — the payoff layout for sources that cut in and out of a
   composited demo.

If no content span is detected in a clip, triptych degrades silently to plain
reframe. A relocated cold-open hook currently disables the overlay for that clip.

## Config

Regions are in **SOURCE pixels** and are **per-source** — they depend entirely on
where that video's editor placed the faces/content. Measure them off a real frame:

```bash
ffmpeg -ss T -i src -vf "drawgrid=width=192:height=108:color=red" f.jpg
```

gives a 10×10 grid on a 1920×1080 frame.

### Triptych (content-switch) — `[triptych]`

This is the **live, working config** from
`projects/Live-Trading-5-Miliar-Jadi-Berapa-Tito-H/config.toml`:

```toml
[clip]
fit = "triptych"

[triptych]
enabled = true
blur_sigma = 25
top    = { x = 160,  y = 378, w = 665, h = 327 }   # left face  -> top band (cover)
phone  = { x = 735,  y = 70,  w = 445, h = 915 }   # content    -> mid band (contain+blur)
bottom = { x = 1225, y = 470, w = 665, h = 327 }   # right face -> bottom band (cover)
bands  = { top = 520, mid = 880, bottom = 520 }    # output band heights, sum = 1920

[triptych.detect]                                  # what turns the overlay on
region = { x = 760, y = 1015, w = 400, h = 50 }    # sample area (the sponsor banner)
green_over_red = 20   # banner is green: G beats R by this; talking heads are warm (R wins)
min_green = 60
max_red = 130
sample_fps = 4.0
min_seconds = 1.2     # ignore banner blips
merge_gap = 1.0       # bridge short gaps into one span
pad = 0.12            # grow each span so the switch doesn't clip the cut
```

### Duo / streamer (always-on) — `[stack]`

```toml
[clip]
fit = "stack"

[stack]
enabled = true
always = true                      # whole clip (no content detection)

# duo: two people stacked
[[stack.panels]]
rect = { x = 400,  y = 230, w = 600, h = 534 }   # top person
height = 960
mode = "cover"
[[stack.panels]]
rect = { x = 1280, y = 200, w = 600, h = 534 }   # bottom person
height = 960
mode = "cover"

# streamer would instead be: facecam (cover) on top, game (contain) below —
# e.g. facecam rect at height 700 mode cover, game rect at height 1220 mode contain.
```

## When this works — and when it doesn't (read before promising it)

The trick is **finding the panel regions**. There are three regimes:

- **Fixed composite** (the trading app demo): the editor already laid the faces and
  the phone side-by-side in fixed spots. Fixed rects nail it, and banner detection
  drives a clean face↔content switch. *This is the ideal case.*
- **Locked single camera angle**: a two-shot from one static camera. Fixed rects
  work for that angle. If the edit cuts between angles, you need a rect set per
  angle (and a way to know which is live) — otherwise the crops grab the wrong
  thing after a cut.
- **Live multi-cam with small, moving faces** (a 4-person podcast wide shot): fixed
  rects only match one arrangement, and the blazeface short-range detector misses
  the small/distant faces, so it can't follow the speakers automatically. Honest
  answer: a robust auto duo here needs a stronger/long-range face model plus
  active-speaker detection, or hand-authored per-angle regions. Don't promise
  automatic 2-speaker tracking on this footage — prototype it and show the limit.

So: **fixed regions are reliable when the thing you're cropping doesn't move.** The
further the source is from a stable composite, the more manual (or the better a
face model) it needs. Prototype the look on one frame before committing a project to
a stack layout.

### A vs B for multi-cam podcasts (the PKA question)

- **B — fixed regions**: hand-tuned crop boxes. Perfect on the tuned angle, but
  wrong after any camera cut (frames a bookshelf, shrinks the people) and never
  follows a different speaker. Needs shot-detection + one box-set per angle to
  survive an edit. Low code, brittle, manual per video.
- **A — auto follow speaker**: detect faces in the current frame, pick the active
  speaker(s), crop and stack. Adapts across every cut and follows whoever talks.
  Needs a stronger long-range face model + active-speaker detection; higher build
  effort and jitter risk, but the only real solution for multi-cam.

B is quick and looks great on one locked angle; A is the right answer for real
podcast multi-cam. Neither is built for A yet — B works today only on stable
angles.

## Implementation notes

- Engine: `clipcraft/triptych.py`. `panels(cfg)` resolves the panel list (explicit
  `[stack].panels`, else the `[triptych]` preset); `stack_subgraph()` builds the
  ffmpeg `split → per-panel crop/scale → vstack` graph; `overlay_tail()` overlays it
  on a base gated by time spans; `resolve_spans()` returns the whole clip (always-on)
  or the detected content spans (triptych).
- Integrated in `clipcraft/render.py` across the freeze-TTS, title-card, and plain
  branches. Captions are burned **after** the overlay so they sit above the stack
  (this matters — default captions land ~0.40 of height, i.e. over the middle band).
- `fit = "stack"` uses a cheap center-crop as the (hidden) base and overlays the
  always-on stack over it, reusing the exact triptych machinery.
- Content detection (`detect_phone_spans`) samples the banner region at 4 fps and
  discriminates by colour: the sponsor banner is green (G−R > threshold), talking
  heads are warm (R wins). Hits → merged/padded/min-duration time spans.
