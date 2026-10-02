# Auto-runner — unattended clip generation

Turns the pipeline from "run every step myself" into **drop a video, come back to
clips ready for review**. The heavy compute (transcribe → candidates → render)
runs unattended on this machine; you only do the cheap, high-value part —
**fixing titles and posting**. Nothing goes public on its own.

## Why local (not a cloud cron)

`transcribe`, `candidates` (Ollama), and `render` (ffmpeg + face detection) need
this machine's GPU, models, and the source video files. A cloud scheduled agent
has none of that, so automation lives here as Windows Scheduled Tasks driving
`auto_runner.py`.

## The loop

```
projects/<name>/inbox/           <- you drop a source video here
        │  (picked up within ~60s, or at the nightly 03:00 sweep)
        ▼
   clipcraft run --project <name> --source <file> --name <stem>
        │
        ├─ output/<stem>/clips/…      finished 9:16 clips (model-picked, model titles)
        ├─ output/<stem>/_REVIEW.txt  the `review` digest (scores, times, titles, hooks, captions)
        ├─ source/<file>              processed source moved here (never reprocessed)
        └─ _REVIEW_QUEUE.md           a new "[ ] <stem> — fix titles, then post" line
```

On error the source is moved to `inbox/_failed/` and the loop keeps going; the
full run log is in `_runner/logs/<project>__<stem>.log`.

## Your part (the gate that stays manual, by design)

For each unchecked line in a project's `_REVIEW_QUEUE.md`:

1. `py -3 -m clipcraft review --project "<name>" --name "<stem>"` (or open `_REVIEW.txt`).
2. **Fix titles** to satisfy the title rule (who + what + source — the model drifts
   to vague/English titles), edit any `.srt` caption text, then
   `py -3 -m clipcraft recut --project "<name>" --name "<stem>" --clip N`.
3. Post: `upload` (YouTube) / `tiktok` (drafts) / `telegram` (to phone for Reels).
4. Tick the box in `_REVIEW_QUEUE.md`.

## Triggers (Windows Scheduled Tasks)

| Task | Schedule | What it does |
|------|----------|--------------|
| `ClipSystem-Watch`   | every 1 min | `auto_runner.py --once` — near-instant pickup |
| `ClipSystem-Nightly` | daily 03:00 | `auto_runner.py --once` — safety net if the PC was asleep |

Install / update / remove:

```powershell
powershell -ExecutionPolicy Bypass -File install_tasks.ps1
powershell -ExecutionPolicy Bypass -File install_tasks.ps1 -Uninstall
schtasks /Run  /TN ClipSystem-Watch     # kick a sweep now
schtasks /Query /TN ClipSystem-Watch /FO LIST
```

Run manually any time (no scheduler): `py -3 auto_runner.py --once`
or `py -3 auto_runner.py --watch --interval 60`.

## Notes & limits

- **One source = one job**, named after the file stem (`--name <stem>`), so jobs
  don't collide in `output/`. Putting several very different videos through the
  *same* project still shares that project's config (watermark, outro, brief) and
  can collide `raw_cuts/`/`main_content/` (known multi-video caveat) — prefer one
  project per show/channel.
- **New projects** need an `inbox/` folder (`mkdir projects/<name>/inbox`); the
  sweep only touches projects that have one.
- The runner deliberately stops at rendered clips. It does **not** auto-title or
  auto-post, because both violate the house title/review rules if done blind.
- Per-project lock (`_runner/locks/<name>.lock`) guarantees two sweeps never run
  the same project at once (honors the manifest concurrent-write rule); a lock
  older than 6h is treated as abandoned and reclaimed.
