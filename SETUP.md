# ClipCraft — Setup

Standalone service repo. Code only — no source videos, no project data, no
secrets. You bring your own inbox, credentials, and Ollama model.

## Prerequisites

- **Python 3.10+**
- **ffmpeg** on `PATH` (`ffmpeg -version` should work)
- **Ollama** running locally for moment selection — `ollama serve`, then
  `ollama pull qwen2.5:7b`
- **NVIDIA GPU (optional)** — faster-whisper uses it if present; CPU works but is
  slow on long sources. CUDA 12 wheels are pinned in `requirements.txt` and are
  harmless on CPU-only machines.

## Install

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# *nix:     source .venv/bin/activate
pip install -r requirements.txt

# Reaction detection ([reaction]) wants a CPU torch build to avoid the ~2.5GB CUDA one:
pip install torch --index-url https://download.pytorch.org/whl/cpu

python -m clipcraft doctor   # verifies ffmpeg / GPU / Ollama / model tier
```

## Secrets — set via environment, never commit

The tracked `config.toml` ships these blank on purpose. Provide them at runtime:

| What | Env var | Needed for |
|------|---------|-----------|
| HuggingFace read token | `HF_TOKEN` | `[diarize]` pyannote (gated model, one-time "Agree" on the model page) |
| TikTok app key/secret | `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` | `clipcraft tiktok` upload |
| Telegram bot | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `[telegram]` distribution |
| YouTube Data API key | `YOUTUBE_API_KEY` | `discover.py` source finding |

YouTube upload OAuth (`client_secret*.json`, `.youtube_token*.json`) and the
Google Sheets `service_account.json` are `.gitignore`d — drop your own copies at
the repo root; they are never committed.

## First run

```bash
# 1. Candidate moments only (no render) — sanity-check the judgment layer
python -m clipcraft candidates --source "<youtube-url>" --n 10

# 2. Full pipeline: transcribe -> select -> snap -> render 9:16 clips
python -m clipcraft run --source "<youtube-url>" --n 10

# 3. Review a run cheaply (scores, times, titles, caption text)
python -m clipcraft review

# 4. Fix a clip: edit clip_NN.srt, then
python -m clipcraft recut --clip N
```

Per-project config, inboxes, and outputs live under a `projects/<name>/` tree
(git-ignored here) — see `config.toml` and `docs/` for the full model.

## More

- `README.md` — overview and rationale
- `COMMANDS.md` — full command reference
- `CLAUDE.md` — house rules and design invariants enforced in `clipcraft/`
- `docs/` — stack layouts, auto-runner, discover, editor export
