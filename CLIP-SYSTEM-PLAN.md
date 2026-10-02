# ClipCraft — Build Plan (Clip System)

Local-first video clipping. Brief in, compliant vertical clips out.

---

## 0. What "without touching code" actually means

Being precise so there are no surprises later.

**I write 100% of the code.** Every module, test, config file, and the setup script. You will never open an editor.

**You run commands.** Four of them, once:

```bash
git clone <repo> && cd clipcraft
./setup.sh                    # installs deps, pulls models
python -m clipcraft doctor    # verifies your machine
python -m clipcraft run --brief brief.png --source "<url>" --n 10
```

**What I cannot do from my sandbox**, and why the plan is built around it:

| Blocked | Consequence |
|---|---|
| No YouTube access | I can't test ingestion on a real video — I test with generated fixtures |
| No Hugging Face access | I can't download Whisper/TTS weights or verify their output |
| No GPU | I can't measure real transcription speed |
| Sandbox resets | Nothing persists between sessions except what you download |

**So the split is:**

| Layer | Who verifies | Confidence at handoff |
|---|---|---|
| Boundary snapping, ledger, timeline IR, compliance rules, brief parsing | Me, with unit tests | High — pure logic, fully tested |
| ffmpeg cut / caption / crop / quality gates | Me, on synthetic video | High — already proven working |
| Whisper transcription | You, first run | Medium — standard API, low risk |
| LLM selection quality | You, first run | This is the real unknown |
| Auto-reframe on real faces | You | Low until tested on your footage |

The honest summary: I can hand you something that installs and runs. Whether the *clips are good* is the one thing neither of us knows until it runs on your footage. That's why Session 1 exists.

---

## 1. Architecture

Single Python package, SQLite for state, local disk for artifacts. No server, no queue, no cloud.

```
clipcraft/
├── setup.sh                  # one-shot install
├── requirements.txt
├── config.toml               # user-editable, no code
├── clipcraft/
│   ├── cli.py                # single entry point, all commands
│   ├── doctor.py             # environment check w/ actionable errors
│   ├── config.py
│   ├── db.py                 # sqlite: jobs, artifacts, cache
│   ├── ingest.py             # yt-dlp + local file + Instagram
│   ├── transcribe.py         # faster-whisper, cached by content hash
│   ├── brief.py              # screenshot/text → constraints JSON
│   ├── select.py             # LLM candidate selection
│   ├── snap.py               # boundary correction        [BUILT]
│   ├── compliance.py         # rule gate, flags not blocks
│   ├── timeline.py           # the IR both pipelines emit
│   ├── render.py             # cut/caption/crop/loudness  [BUILT]
│   ├── reframe.py            # face track → crop path
│   ├── ledger.py             # learning loop              [BUILT]
│   └── review.py             # local web UI on :7788
├── tests/
└── fixtures/                 # synthetic video+transcript for CI
```

**Key decisions:**

- **Content-hash caching.** Re-render with different captions must not re-run Whisper. Every artifact keyed by hash of (input hash + params).
- **Timeline IR is mandatory from day one.** Even though only the clipper uses it initially. Retrofitting it later costs a week; adding it now costs a day, and the voice-shorts system reuses the entire renderer for free.
- **Compliance flags, never blocks.** The gate marks clips and requires your sign-off. An auto-approving gate that's wrong 5% of the time is worse than useless.
- **SQLite over Postgres.** Single user, single machine. Zero setup, one file to back up.

---

## 2. Sessions

Each session is one chat with me, ends with something runnable. Sized to fit comfortably in a single context window.

### Session 1 — Prove the judgment layer (highest priority)

Nothing else matters if this fails. Build the thinnest path from video to candidate list.

Deliver: `ingest.py`, `transcribe.py`, `select.py`, `snap.py`, `cli.py` with one command.

```bash
python -m clipcraft candidates --source "<youtube-url>" --n 10
```

Outputs a JSON list plus a readable table of candidates with hooks, scores, and reasons. No rendering yet.

**Gate:** you read the 10 candidates for a video you know. If fewer than 3 are ones you'd post, we iterate on the prompt and rubric right here — cheap, minutes per loop. Do not proceed until this passes.

### Session 2 — Render + review UI

Deliver: `timeline.py`, extended `render.py`, `review.py`, `db.py`.

```bash
python -m clipcraft run --source "<url>" --n 10
python -m clipcraft review          # opens localhost:7788
```

Local web UI: candidate list, inline player, trim handles, accept/reject with a reason dropdown. Accepting renders the final file; rejecting writes to the ledger.

**Gate:** you clip one real video end to end and stop opening a video editor.

### Session 3 — Brief parsing + compliance

Deliver: `brief.py`, `compliance.py`, campaign mode.

```bash
python -m clipcraft run --brief brief.png --materials materials.txt --n 10
```

Brief screenshot goes to a vision model, comes back as structured constraints. The parser reports contradictions rather than guessing — your MGS5 EV vs MG ZS HYBRID+ case would halt with a question instead of producing a wrong batch.

Compliance gate, honestly scoped:

| Rule | Method | Reliability |
|---|---|---|
| Source from approved materials | Hash allowlist | Certain |
| Edit layer present | Structural check | Certain |
| Target vehicle mentioned | Transcript match | High |
| Positive sentiment on target | LLM classify | Good |
| No SARA content | Classifier + wordlist | Good, not perfect |
| No competitor brands | Transcript + optional OCR | Weak |
| No trending controversy | — | Not automatable locally |

Output is a per-clip report with PASS / FLAG / FAIL and reasons. You sign off.

### Session 4 — Auto-reframe

Deliver: `reframe.py` — YOLOv8-face, ByteTrack, active-speaker, and the smoothing stack (deadband, hold-on-switch, low-pass, velocity clamp).

**Gate:** dynamic beats static on 7 of 10 clips and never looks broken. If it can't clear that, we ship static permanently. That is a legitimate outcome, not a failure — static crop is fine for single-speaker content and half of commercial tools quietly do exactly this.

### Session 5 — Close the loop

Deliver: distillation pass, sqlite-vec retrieval of similar shipped hooks, improvement dashboard.

```bash
python -m clipcraft distill
python -m clipcraft stats
```

**Gate:** clean ship rate measurably above your Session 2 baseline.

---

## 3. Timeline (realistic)

| Session | My work | Your work | Elapsed |
|---|---|---|---|
| 1 | ~1 chat | 1 hr reviewing candidates | Day 1 |
| 2 | ~2 chats | 1 hr clipping a real video | Day 3–4 |
| 3 | ~2 chats | 1 hr checking compliance output | Day 6–7 |
| 4 | ~2 chats | 1 hr watching reframe A/B | Day 9–11 |
| 5 | ~1 chat | ongoing use | Day 12–14 |

**~2 weeks wall clock**, of which maybe 5 hours is yours. Faster than my earlier estimate because dropping the server tier removes most of the work.

---

## 4. Hardware

| Your machine | 1hr source → transcript | Verdict |
|---|---|---|
| NVIDIA GPU (8GB+) | ~4 min | Ideal, everything works |
| Apple Silicon | ~6 min (whisper.cpp Metal) | Good; reframe is slow but usable |
| CPU only, 16GB | 40+ min | Works, painful. Use `medium` model |
| CPU only, 8GB | — | Not viable |

`doctor` detects this and configures the model tier automatically.

---

## 5. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| LLM selection is mediocre on your content | Medium | Session 1 gate catches it in hours, not weeks |
| Instagram ingestion breaks | High, recurring | Manual download fallback from day one |
| Reframe never looks good enough | Medium | Ship static, it's a valid endpoint |
| Whisper Indonesian accuracy is weak | Medium | `large-v3` handles Indonesian well; test in Session 1 |
| Compliance gate misses a violation | Certain, eventually | Never auto-approve — human sign-off always |

The one that matters is the first. Everything else has a workaround.

---

## 6. Start here

Reply with:

1. Your hardware (GPU / Apple Silicon / CPU-only, and RAM)
2. One YouTube URL you know well enough to judge candidates against

I'll build Session 1 in that chat and you'll have a candidate list to react to the same day.
