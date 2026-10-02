# ClipCraft — Learning & Boundary Plan

How the system gets better at **knowing where to clip**, and how it **learns that from you** instead of guessing the same way forever.

This is the layer-3 work from the main plan (`CLIP-SYSTEM-PLAN.md`). The pipeline already runs; this document is about making the *judgment* improve over time. It is research-backed — sources are listed at the end and referenced inline as `[n]`.

---

## 0. Where we are today (the honest baseline)

The shipped pipeline is **stateless**. Every run decides fresh from three fixed sources:

| Decision | Lives in | Learns? |
|---|---|---|
| Which moment to clip | `llama3.1:8b` + a fixed prompt/rubric in `select.py` | No |
| Exact start/end | sentence+word rules in `snap.py` | No |
| Padding & length | `config.toml` (`lead_in`, `tail`, 15–60s) | Only if you hand-edit |

If you reject or trim a clip, nothing is recorded and the next run repeats the mistake. Closing that gap is the entire point of this plan.

Two separate problems, often conflated:

- **Problem A — Where to clip better** (perception + judgment quality, *independent* of you).
- **Problem B — Learn from feedback** (adapt A to *your* taste and *your* content).

They need different solutions. A is better signals and boundary math; B is a feedback loop. We build A's cheap wins first because they raise the floor before any learning starts.

---

## 1. What the research says

### 1a. Where good clips start and stop

- **The first 3 seconds decide everything.** ~71% of viewers decide whether to keep watching within 3s; ~65% who pass 3s reach 10s. Watch-time and completion are the ranked signals, and both are won or lost in the opening [5]. Implication: **the clip must start *on* the hook, not 2s before it.** A slow build ("let me give context…") is death for shorts [5].
- **Hooks that work are structural:** bold claims, a question that opens a curiosity gap, a strong self-contained statement, a number/result stated up front [5]. These are detectable in text.
- **Boundaries follow speech structure, not fixed chunks.** State-of-the-art long-video highlight selection (TF-SELECTOR / SVHighlights) segments by merging shots that share semantic content, using **word-level ASR timestamps**, treating consecutive words with a **<1s gap as one sentence**, and scoring segments with an LLM fed **transcript + audio volume + visual captions** [3].
- **Silence is the safest cut.** General video segmentation uses a **silence-cut-cost inversely proportional to adjacent silence duration** — cut where the pause is longest, because that is where a thought actually ended [3].
- **Expect to discard 20–40%.** Even the market leader's virality score is a weak predictor; a low-scored clip sometimes pops and a high one dies [1]. So the target is *"a strong shortlist a human picks from,"* never *"auto-ship."* Over-propose.

### 1b. How to learn from a single user's feedback

- **Every edit is implicit pairwise preference.** "Shipped A, rejected B" and "moved the start from 12.0s → 12.8s" are ranking signals. Learning-to-rank from implicit feedback (clicks, edits) is a mature area; the core trick is pairwise: the chosen item outranks the examined-but-rejected one [2].
- **At small data, don't fine-tune — retrieve.** In-context learning with *retrieved* exemplars lifts LLM task performance ~7.8% over random examples, at any model size, with **zero parameter updates** [4]. For a single creator producing tens of sessions, this beats training a model: embed your shipped hooks, retrieve the most similar past winners, and inject them as few-shot examples into the selection prompt [4].
- **Numeric habits are just statistics.** Your trim deltas (how far you move each start/end) are a distribution. A running estimate of that distribution *is* the learned calibration — no ML needed for the highest-value win.
- **Exploration prevents a taste echo-chamber.** Pure exploitation of past preferences creates a feedback loop that pigeonholes the system and never discovers what you'd actually like; deliberate exploration (ε-greedy / bandit) fixes this [6]. Concretely: always slip a couple of "off-profile" candidates into the shortlist so the ledger keeps learning.

---

## 2. The learning loop (architecture)

```
run → propose (select.py) → you review (accept / reject / trim + reason)
                                        │
                                        ▼
                         .clipcraft/ledger.jsonl          (raw events, append-only)
                                        │  distill (batch, every ~10 sessions)
                                        ▼
        calibration.json  +  hooks.md  +  exemplars.sqlite (vec)   (learned house rules)
                                        │  loaded at start of next run
                                        ▼
            select.py prompt (few-shot exemplars + rules)  •  snap.py (learned padding)
```

Three artifacts, three different learning mechanisms, deliberately kept separate so each can be inspected and rolled back:

| Artifact | Holds | Mechanism | Data needed |
|---|---|---|---|
| `calibration.json` | numeric habits: lead-in, tail, length, silence sensitivity | running statistics on edit deltas | ~5 sessions |
| `hooks.md` | your do/don't rules, hook types you ship vs cut | LLM distillation of rejection reasons | ~10 sessions |
| `exemplars` (sqlite-vec) | embeddings of shipped hooks + their transcripts | retrieval → few-shot in prompt | ~20 clips |

Everything is **local, append-only, and human-readable** (except the vector index). You can open the ledger and see exactly why a rule exists — no black box.

---

## 3. What we record (the ledger schema)

One JSON line per proposed candidate, per session. Recording the **negative** examples matters most — rejections teach more than accepts.

```jsonc
{
  "session": "2026-08-28T14:03_dQw4",
  "source": "https://youtu.be/…",
  "candidate_id": 3,
  "proposed":  {"start": 412.3, "end": 461.8, "hook": "…", "score": 8.2},
  "features":  { /* see §5 — signals present at proposal time */ },
  "outcome":   "shipped | rejected | edited",
  "final":     {"start": 413.1, "end": 460.9},        // null if rejected
  "delta":     {"start": +0.8, "end": -0.9},           // the learning signal
  "reason":    "started_too_early",                    // controlled vocabulary
  "explore":   false                                    // was this an exploration pick?
}
```

Reason vocabulary (cheap to give, one tap): `started_too_early`, `started_too_late`, `ended_short`, `ended_long`, `needs_context`, `weak_hook`, `off_topic`, `great` — free text optional. Never ask for essays.

---

## 4. Problem A — knowing *where* to clip better (v2 boundaries)

These land **before** any learning and raise the floor for everyone. Ordered by value-per-effort.

1. **Hook-first scoring.** Change `select.py` to score the *opening line* explicitly against the hook taxonomy [5]: is the first sentence a claim/question/result, or a slow build? Penalize builds. This directly targets the 3-second rule.
2. **Silence-aware snapping.** Extract a silence/energy track from the audio (ffmpeg `silencedetect` or an RMS envelope). In `snap.py`, prefer the boundary adjacent to the **longest nearby pause** (silence-cut-cost) [3], not just the nearest `.`/`!`/`?`. Punctuation and silence together beat either alone.
3. **`<1s gap = same sentence` grouping** [3] as a fallback when Whisper punctuation is weak (esp. Indonesian), so boundaries never split a spoken sentence.
4. **Audio-energy saliency (cheap multimodal).** Add mean/peak loudness of a segment as a feature — emotional peaks correlate with highlights and cost almost nothing [1][3].
5. **Later, opt-in:** visual-caption saliency and face/scene cues [3] — heavier, deferred until the text-only path is proven.

The principle from the main skill still governs: **stability beats accuracy, over-propose, and a clip that needs earlier context fails regardless of how good the moment is.**

---

## 5. Problem B — the learning, phased by how much data exists

Each phase is shippable and gated. Do not build phase N+1 until phase N measurably helps.

### Phase 0 — better priors (no data yet)
Ship the §4 boundary work and a stronger default rubric. This is the "cold-start" quality. **Gate:** clean-ship rate beats the current baseline on a video you know.

### Phase 1 — calibration + rules (≈1–20 sessions)
- Build `review` (CLI first, web later) to capture accept/reject/trim → ledger.
- **Calibration:** after each session, update running medians of start/end deltas → write `lead_in`, `tail`, target length into `calibration.json`, which `snap.py` reads *over* `config.toml`. If you always shave 0.8s off the front, clips start 0.8s later automatically.
- **Rule distillation:** feed rejection reasons to `llama3.1:8b`, emit 5–10 bullet house rules into `hooks.md`, injected into the selection system prompt.
- **Gate:** median trim distance shrinks across sessions (you're editing less).

### Phase 2 — retrieval-augmented selection (≈20+ clips)
- Embed every shipped hook + its transcript (local embedding model via Ollama) into **sqlite-vec**.
- At selection time, retrieve your top-k most similar *past winners* and inject them as few-shot exemplars [4]. The model now proposes "more like what Fauzan actually ships."
- **Gate:** blind A/B — retrieval-augmented shortlist beats the plain prompt on clean-ship rate.

### Phase 3 — a learned re-ranker (≈100+ labelled candidates)
- Train a small **logistic/gradient-boosted scorer** on ledger features (§5 table) predicting `shipped vs rejected` — a pairwise ranker [2]. Use it to *re-rank* the LLM's candidates, not replace them.
- Only if data justifies it: **DPO** preference fine-tune of the local model on your shipped-vs-rejected pairs. This is the last resort — retrieval [4] usually gets 80% of the benefit at 5% of the effort.
- **Gate:** re-ranker's top-3 contains more of your eventual ships than the LLM's raw top-3.

### Cross-cutting — exploration (from Phase 1 on)
Reserve ~20% of shortlist slots for **exploration picks** — candidates deliberately outside your learned profile, tagged `explore:true` in the ledger [6]. Without this, the loop overfits to your first few sessions and stops surfacing anything new.

### Feature table (recorded per candidate for Phases 1–3)
| Feature | Why it predicts a ship |
|---|---|
| hook type (claim/question/result/build) | 3-second rule [5] |
| opening-line length, first-word timing | slow-build detector [5] |
| segment mean/peak loudness | emotional peak proxy [1][3] |
| longest adjacent silence | clean-cut availability [3] |
| self-contained score (LLM 0–1) | "needs context" is the top rejection reason |
| length, words-per-second | pacing |
| LLM score | current baseline |

---

## 6. How we know it's actually working (metrics)

Vanity metrics lie. Track these in `clipcraft stats`, per session and trended:

- **Clean-ship rate** — % of proposed clips shipped with *no* trim. The headline number.
- **Median trim distance** — seconds moved on shipped clips. Should fall as calibration learns.
- **Rejection-reason mix** — `needs_context` and `started_too_early` should shrink specifically.
- **Shortlist hit rate** — of clips you ship, how many were in the top-3 proposed.
- **Exploration yield** — % of exploration picks that got shipped (proves the loop isn't a bubble).

Honest ceiling [1]: interview / clear Q&A content automates toward "great shortlist" fast; rambling or visually-driven content stays "human picks from a shortlist." We never promise auto-ship, and we never auto-approve.

---

## 7. Milestones → modules

| Milestone | New/changed | Depends on |
|---|---|---|
| M1 boundary v2 | `snap.py` (silence-cut), `select.py` (hook-first), audio-energy util | — |
| M2 capture | `review.py` (CLI), `ledger.py` (record) | — |
| M3 calibrate | `ledger.py distill`, `calibration.json` read in `snap.py` | M2 |
| M4 rules | rule distillation → `hooks.md` in prompt | M2 |
| M5 retrieval | embeddings + sqlite-vec, few-shot injection | M2, ~20 clips |
| M6 re-rank | feature logger + trained scorer | M2–M5, ~100 labels |
| — exploration | ε-greedy slot in `select.py` | M2 |

Build order: **M1 → M2 → M3** delivers a system that visibly learns your timing within a week. M4–M6 compound after data accrues.

---

## 8. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Over-fits to first few sessions | High without exploration | ε-greedy exploration slots from Phase 1 [6] |
| Feedback is too sparse to learn | Medium | Calibration works at ~5 sessions; retrieval degrades gracefully to the plain prompt |
| Silence detection misfires on music beds | Medium | Combine silence with punctuation; never rely on one signal [3] |
| Learned rules drift wrong | Low | Everything human-readable + versioned; roll back a bad `hooks.md`/`calibration.json` |
| Chasing a virality score that doesn't predict | Certain if we try | We rank for *your* ships, not a global virality guess [1] |

---

## 9. Start here

M1 + M2 are independent and both cheap. Recommended first step: **M2 (capture)** — even before any learning logic, a `review` command that records accept/reject/trim starts building the dataset every other phase needs. A week of your normal clipping then makes M3 calibration real instead of theoretical.

---

## Sources

1. [What is the Virality Score on OpusClip](https://help.opus.pro/docs/article/virality-score) · [Opus Clip Tested 2026 (40% you'll discard)](https://bigvu.tv/blog/opus-clip-tested-2026-where-ai-wins-40-percent-discard/) · [Find viral moments with AI](https://streamyard.com/blog/find-viral-moments-in-video-ai)
2. [Query Chains: Learning to Rank from Implicit Feedback (Radlinski & Joachims)](https://www.cs.cornell.edu/people/tj/publications/radlinski_joachims_05a.pdf) · [Learning Neural Ranking Models Online from Implicit User Feedback](https://arxiv.org/pdf/2201.06658)
3. [SVHighlights / TF-SELECTOR: training-free long-video highlight detection](https://arxiv.org/html/2606.06926) (word-level ASR sentence grouping, silence-cut-cost, LLM segment saliency over transcript + audio volume + visual captions)
4. [Learning to Retrieve In-Context Examples for LLMs](https://arxiv.org/pdf/2307.07164) · [Many-Shot In-Context Learning](https://arxiv.org/pdf/2404.11018) · [Few-shot Prompting for Pairwise Ranking](https://arxiv.org/pdf/2409.17745)
5. [TikTok 3-Second Rule](https://www.teleprompter.com/blog/tiktok-3-second-rule) · [YouTube Shorts hook formulas](https://www.opus.pro/blog/youtube-shorts-hook-formulas) · [Ideal Shorts length & retention (data-backed)](https://www.opus.pro/blog/ideal-youtube-shorts-length-format-retention)
6. [Exploration vs. Exploitation in Recommendation Systems (Shaped)](https://www.shaped.ai/blog/explore-vs-exploit) · [Bandits for Recommender Systems (Eugene Yan)](https://eugeneyan.com/writing/bandits/)
