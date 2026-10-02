# Clip System — n8n integration

Automation glue **around** the clipcraft pipeline. The heavy work (whisper,
selection, ffmpeg render, uploads) still runs in `clipcraft/` exactly as the CLI
runs it. This layer adds: external triggers, a **Discord approval gate**, drip
scheduling, multi-platform fan-out, and a Google-Sheets ledger.

```
                 ┌────────────── host (your PC) ──────────────┐
  YouTube RSS ─▶ │  clipcraft API  :8756   Discord bot :8757  │ ◀─▶ Discord
   / schedule    │   (wraps the CLI)        (approve buttons)  │
        │        └───────────▲───────────────────▲────────────┘
        │                    │ host.docker.internal│
        ▼                    │                     │
   ┌─────────────── n8n (Docker :5678) ───────────────┐
   │ trigger → run → wait → Discord → wait → publish   │
   │           + drip + fan-out (IG/FB/X) + Sheets log │
   └───────────────────────────────────────────────────┘
```

**Design decision — the gate is on *publishing*, not rendering.** clipcraft's
`run` renders the top-N by score in one shot (no "render only these" mode), and
rendering is local + free. The irreversible outward step is *uploading*. So the
Approve / Reject / Edit-title gate in Discord sits **after** render and **before**
upload/fan-out — which also lets Discord show the actual rendered vertical clip.

---

## Components

| Piece | Where it runs | Port | Start with |
|-------|---------------|------|-----------|
| clipcraft API | host (needs your Python + ffmpeg + GPU) | 8756 | `integrations/run_api.ps1` |
| Discord bot | host | 8757 | `integrations/run_bot.ps1` |
| n8n | Docker | 5678 | `docker compose up -d` |

n8n runs in Docker but the API + bot must run on the host (they invoke the CLI
and ffmpeg). n8n reaches them via `host.docker.internal`; the compose file maps
that to the host gateway.

---

## One-time setup

### 1. Install API + bot deps
Into the **same** interpreter that runs clipcraft (`py -3` here):
```powershell
py -3 -m pip install -r integrations/requirements.txt
```

### 2. Discord bot
1. https://discord.com/developers/applications → New Application → **Bot** → copy token.
2. Invite it to your server with the **Send Messages** permission (OAuth2 → URL
   Generator → scopes `bot`, permission "Send Messages"). No privileged intents needed.
3. Enable Developer Mode in Discord, right-click your target channel → **Copy Channel ID**.

### 3. Config
```powershell
Copy-Item integrations/.env.example integrations/.env
# then fill in DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID (and optionally CLIPCRAFT_API_TOKEN)
```

### 4. Start everything
```powershell
# terminal 1
integrations/run_api.ps1
# terminal 2
integrations/run_bot.ps1
# terminal 3
cd integrations ; docker compose up -d
```
Open http://localhost:5678, create your n8n account, then **Import from File** each
JSON in `integrations/n8n/`.

---

## The API (host :8756)

All heavy calls are async **jobs**: POST returns a `job` immediately; poll
`GET /jobs/{id}` for `status` (`running`/`done`/`failed`), `log_tail`, and `result`.
If you pass `webhook_url`, the API POSTs the finished job to it (this is how n8n's
Wait node resumes).

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/health` | liveness |
| GET  | `/projects` · `/projects/{p}/runs` | discovery |
| GET  | `/projects/{p}/runs/{name}/candidates` · `/manifest` | read artifacts |
| PATCH| `/projects/{p}/runs/{name}/clips/{clip}` `{title}` | edit a title |
| GET  | `/file?path=...` | serve a rendered clip (restricted to `projects/`) |
| POST | `/jobs/candidates` | pick candidates, no render |
| POST | `/jobs/run` | pick + render top-N → `manifest.json` |
| POST | `/jobs/recut` `{clip}` | re-render one clip (after a title edit) |
| POST | `/jobs/upload` | YouTube Shorts (optionally `clips: "1,3,5"`) |
| POST | `/jobs/tiktok` | TikTok drafts |

Body always includes `project` (a folder under `projects/`) and, for reads, the
run `name`. Set `CLIPCRAFT_API_TOKEN` in `.env` to require `?x-token=...` on every call.

Smoke test once running:
```powershell
curl http://localhost:8756/health
curl http://localhost:8756/projects
```

## The Discord bot (host :8757)

n8n calls it; it talks to Discord.

- `POST /notify {title?, content}` — a monitoring message in your channel.
- `POST /approval {resume_url, project, run_name, clips:[{clip,title,start,end,score,file,thumbnail}]}`
  — posts each rendered clip (video attached if ≤ `DISCORD_MAX_UPLOAD_MB`, else a
  thumbnail + path) with **Approve / Reject / Edit title** buttons, plus a
  **Ship approved** control. On "Ship", it POSTs the collected decisions back to
  `resume_url` (an n8n Wait resume URL):
  `{project, run_name, decisions:[{clip,action,title}], approved:[...]}`.

---

## The workflows (`integrations/n8n/`)

| File | Trigger | What it does |
|------|---------|--------------|
| `02_pipeline_with_approval.json` | **Webhook** `/webhook/clip-pipeline` | The core: run → wait for render → Discord approval → wait → YouTube + TikTok + notify. Start here. |
| `01_auto_trigger.json` | RSS (YouTube channel) | New upload → fires `02` via its webhook. Replace `REPLACE_CHANNEL_ID` + the `project` in the Code node. |
| `03_drip_publish.json` | Schedule (every 4h) | Posts one approved clip per tick (public). Edit the `QUEUE` / `RUN` in the Code node. |
| `04_fanout.json` | Webhook `/webhook/clip-fanout` | Push one clip to Instagram Reels + Facebook + X. **See prerequisites below.** |
| `05_sheets_log.json` | Webhook `/webhook/clip-log` | Append run + shipped clips to Google Sheets. Set `REPLACE_SPREADSHEET_ID` + attach Google creds. |

### How `02` resumes without polling
`/jobs/run` is passed `webhook_url = {{ $execution.resumeUrl }}`. When the render
job finishes, the API POSTs the manifest to that URL and the **Wait for Render**
node continues. Same pattern for the Discord approval Wait. One execution flows
top to bottom; no polling loop.

### Testing `02` by hand
With the workflow open, use its webhook **Test URL**, or:
```powershell
curl -X POST http://localhost:5678/webhook-test/clip-pipeline `
  -H "Content-Type: application/json" `
  -d '{"project":"Financial 1","source":"<url-or-file>","name":"test01","n":10,"render_top":2}'
```
(Activate the workflow to use the non-test `/webhook/` path.)

---

## Fan-out prerequisites (04) — read before wiring

These are **not** plug-and-play; each platform needs its own app + tokens, and
the clip must be reachable as a **public video URL** (the Graph API pulls video
from a URL — it will not read your `localhost` file). Options for a public URL:
host the mp4 on cloud storage, or expose `/file` through a tunnel.

- **Instagram Reels / Facebook** — a Meta app + a Business/Creator IG account
  linked to a FB Page; a long-lived page/IG token; the `instagram_content_publish`
  permission (needs App Review). Fill `REPLACE_IG_USER_ID`, `REPLACE_PAGE_ID`,
  and the tokens. FB Reels has a distinct 3-step upload endpoint; the template
  uses the simpler `/videos` post — swap to `/video_reels` if you need true Reels.
- **X** — media upload is a chunked v1.1 flow; the template's tweet node expects a
  `media_id` you've already uploaded. Attach an `X OAuth2` credential in n8n, or
  use a community X node that handles the chunked upload.

If a platform is more trouble than it's worth, delete its node — the others run
independently (they're parallel branches off the webhook).

---

## Not runtime-tested yet
The code compiles and the workflow JSON is valid, but I have **not** booted the
stack end-to-end (fastapi/uvicorn/discord.py aren't installed here, and it needs
your Discord token). After `pip install` + filling `.env`, the `curl /health` and
webhook-test above are the fastest confidence checks.
