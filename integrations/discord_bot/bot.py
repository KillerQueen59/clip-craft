"""Discord approval + monitoring bot for the clip pipeline.

Two directions:
  * inbound (n8n -> bot): an aiohttp server on BOT_HTTP_PORT.
      POST /notify    {content, title?}                 -> plain monitoring message
      POST /approval  {resume_url, project, run_name,
                       clips:[{clip,title,start,end,score,file,thumbnail}]}
                                                        -> post each rendered clip
                       with Approve / Reject / Edit buttons + a "Ship approved"
                       control message.
  * outbound (bot -> n8n): when the user clicks "Ship approved", the bot POSTs
      the collected decisions to the n8n `resume_url` (a Wait-node resume URL):
      {project, run_name, decisions:[{clip, action, title}], approved:[...]}

Run from the repo root:  python -m integrations.discord_bot.bot
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import aiohttp
import discord
from aiohttp import web

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
HTTP_PORT = int(os.environ.get("BOT_HTTP_PORT", "8757"))
MAX_UPLOAD_MB = float(os.environ.get("DISCORD_MAX_UPLOAD_MB", "25"))

intents = discord.Intents.default()
client = discord.Client(intents=intents)

# session_id -> {resume_url, project, run_name, decisions:{clip:{action,title}}}
SESSIONS: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# button views
# --------------------------------------------------------------------------- #
class EditTitleModal(discord.ui.Modal, title="Edit clip title"):
    def __init__(self, session_id: str, clip: int, current: str):
        super().__init__()
        self.session_id = session_id
        self.clip = clip
        self.new_title = discord.ui.TextInput(
            label="Title", default=current, max_length=200,
            style=discord.TextStyle.paragraph)
        self.add_item(self.new_title)

    async def on_submit(self, interaction: discord.Interaction):
        sess = SESSIONS.get(self.session_id)
        if not sess:
            await interaction.response.send_message("Session expired.",
                                                    ephemeral=True)
            return
        title = str(self.new_title.value).strip()
        sess["decisions"][self.clip] = {"action": "approve", "title": title}
        await interaction.response.send_message(
            f"Clip {self.clip}: approved with edited title -> **{title}**",
            ephemeral=True)


class ClipView(discord.ui.View):
    def __init__(self, session_id: str, clip: int, current_title: str):
        super().__init__(timeout=None)
        self.session_id = session_id
        self.clip = clip
        self.current_title = current_title

    def _set(self, action: str, title: str | None = None) -> None:
        sess = SESSIONS.get(self.session_id)
        if sess is not None:
            sess["decisions"][self.clip] = {
                "action": action,
                "title": title if title is not None else self.current_title,
            }

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success,
                       emoji="✅")
    async def approve(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        self._set("approve")
        await interaction.response.send_message(
            f"Clip {self.clip}: approved.", ephemeral=True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger,
                       emoji="❌")
    async def reject(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        self._set("reject")
        await interaction.response.send_message(
            f"Clip {self.clip}: rejected.", ephemeral=True)

    @discord.ui.button(label="Edit title", style=discord.ButtonStyle.secondary,
                       emoji="✏️")
    async def edit(self, interaction: discord.Interaction,
                   button: discord.ui.Button):
        await interaction.response.send_modal(
            EditTitleModal(self.session_id, self.clip, self.current_title))


class ControlView(discord.ui.View):
    def __init__(self, session_id: str):
        super().__init__(timeout=None)
        self.session_id = session_id

    @discord.ui.button(label="Ship approved", style=discord.ButtonStyle.primary,
                       emoji="\U0001f680")
    async def ship(self, interaction: discord.Interaction,
                   button: discord.ui.Button):
        sess = SESSIONS.pop(self.session_id, None)
        if not sess:
            await interaction.response.send_message("Session expired.",
                                                    ephemeral=True)
            return
        decisions = [{"clip": c, **d} for c, d in sorted(sess["decisions"].items())]
        approved = [d["clip"] for d in decisions if d["action"] == "approve"]
        payload = {
            "project": sess["project"],
            "run_name": sess["run_name"],
            "decisions": decisions,
            "approved": approved,
        }
        ok = await _post(sess["resume_url"], payload)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=(f"\U0001f680 Shipped {len(approved)} clip(s): {approved or '(none)'}"
                     + ("" if ok else "  ⚠️ could not reach n8n resume URL")),
            view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary,
                       emoji="✖️")
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        SESSIONS.pop(self.session_id, None)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="✖️ Cancelled.",
                                                view=self)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _post(url: str, payload: dict) -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(20)):
                return True
    except Exception:  # noqa: BLE001
        return False


def _clip_embed(c: dict) -> discord.Embed:
    start, end = c.get("start", 0), c.get("end", 0)
    e = discord.Embed(
        title=c.get("title") or c.get("hook") or f"Clip {c.get('clip')}",
        description=c.get("hook") or "")
    e.add_field(name="Clip #", value=str(c.get("clip")), inline=True)
    e.add_field(name="Time", value=f"{start:.1f}–{end:.1f}s", inline=True)
    e.add_field(name="Score", value=f"{c.get('score', 0):.1f}", inline=True)
    return e


def _attachable(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if p.exists() and p.stat().st_size <= MAX_UPLOAD_MB * 1024 * 1024:
        return str(p)
    return None


# --------------------------------------------------------------------------- #
# aiohttp endpoints (called by n8n)
# --------------------------------------------------------------------------- #
async def handle_notify(request: web.Request) -> web.Response:
    body = await request.json()
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        return web.json_response({"ok": False, "error": "channel not ready"},
                                 status=503)
    content = body.get("content", "")
    if body.get("title"):
        content = f"**{body['title']}**\n{content}"
    await channel.send(content[:2000] or "​")
    return web.json_response({"ok": True})


async def handle_approval(request: web.Request) -> web.Response:
    body = await request.json()
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        return web.json_response({"ok": False, "error": "channel not ready"},
                                 status=503)
    clips = body.get("clips", [])
    session_id = uuid.uuid4().hex[:12]
    SESSIONS[session_id] = {
        "resume_url": body["resume_url"],
        "project": body.get("project", ""),
        "run_name": body.get("run_name", ""),
        "decisions": {},
    }
    project = body.get("project", "")
    run_name = body.get("run_name", "")
    await channel.send(f"__**Review: {project} / {run_name}**__ "
                       f"— {len(clips)} clip(s) rendered, awaiting approval.")
    for c in clips:
        clip_no = c.get("clip")
        embed = _clip_embed(c)
        view = ClipView(session_id, clip_no, embed.title)
        file_path = _attachable(c.get("file"))
        kwargs = {"embed": embed, "view": view}
        if file_path:
            kwargs["file"] = discord.File(file_path)
        else:
            thumb = _attachable(c.get("thumbnail"))
            if thumb:
                kwargs["file"] = discord.File(thumb)
            if c.get("file"):
                embed.add_field(name="File (too large to attach)",
                                value=f"`{c['file']}`", inline=False)
        await channel.send(**kwargs)
    await channel.send(
        content="Click **Ship approved** when done reviewing above.",
        view=ControlView(session_id))
    return web.json_response({"ok": True, "session_id": session_id})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "ready": client.is_ready(),
                             "sessions": len(SESSIONS)})


async def start_http() -> None:
    app = web.Application()
    app.router.add_post("/notify", handle_notify)
    app.router.add_post("/approval", handle_approval)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    print(f"[bot] http server on :{HTTP_PORT}")


@client.event
async def on_ready() -> None:
    print(f"[bot] logged in as {client.user} — watching channel {CHANNEL_ID}")


async def main() -> None:
    async with client:
        await start_http()
        await client.start(TOKEN)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
