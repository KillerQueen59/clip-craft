"""Push rendered clips to a Telegram chat so they land on your phone.

Instagram Reels has no draft-via-API path for a personal account, so the practical
flow is: the pipeline sends each clip (with a ready-to-paste caption) to your
Telegram chat via a bot; on your phone you save the video and finish posting in
the Instagram app. Zero platform review, works today.

Setup:
  1. Create a bot: message @BotFather -> /newbot -> copy the token.
  2. Open a chat with your new bot and send it any message.
  3. `clipcraft telegram --whoami`  -> prints the chat_id from that message.
  4. Put bot_token + chat_id in [telegram] in config.toml (or env
     TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).
  5. `clipcraft telegram`           -> sends the run's clips to that chat.

The standard Telegram Bot API caps uploads at 50 MB; clips are normally well under
that. Anything over is skipped with its path so you can grab it from output/.
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .config import Config

API = "https://api.telegram.org/bot{token}/{method}"
MAX_BYTES = 50 * 1024 * 1024  # Telegram Bot API upload cap for sendVideo/sendDocument.
CAPTION_LIMIT = 1024  # Telegram caption hard limit.


def _creds(cfg: Config) -> tuple[str, str]:
    tg = cfg.raw.get("telegram", {})
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or str(tg.get("chat_id", ""))
    if not token:
        raise RuntimeError(
            "Missing Telegram bot_token. Create a bot via @BotFather, then put the "
            "token in [telegram] in config.toml, or set env TELEGRAM_BOT_TOKEN.")
    return token, chat_id


def _resolve_dest(cfg: Config, channel: str | None = None
                  ) -> tuple[str, int | None, str | None]:
    """Pick the destination for this send: (chat_id, topic_id_or_None, name_or_None).

    Priority: an explicit --channel > the project's [telegram] channel > the env/
    config default chat_id. A named niche is looked up in [telegram.channels] and,
    being an explicit choice, overrides TELEGRAM_CHAT_ID. A niche entry may be:
      * a chat_id string  -> a SEPARATE channel:            pojok-bola = "-100123"
      * a table {topic=N} -> a TOPIC in one shared forum group (uses [telegram]
        group_id as the chat), or {chat_id="-100…", topic=N} to name the group
        inline:                                             pojok-bola = { topic = 7 }
    """
    tg = cfg.raw.get("telegram", {})
    channels = tg.get("channels", {}) or {}
    group_id = str(tg.get("group_id", "")).strip()
    name = channel or tg.get("channel")
    if name:
        entry = channels.get(name)
        if isinstance(entry, dict):
            cid = str(entry.get("chat_id", "") or group_id).strip()
            topic = entry.get("topic")
            if not cid:
                raise RuntimeError(
                    f"telegram channel '{name}' has a topic but no group to post it "
                    f"to. Set [telegram] group_id to your forum supergroup id, or add "
                    f"chat_id to the entry.")
            return cid, (int(topic) if topic is not None else None), name
        cid = str(entry or "").strip()
        if cid:
            return cid, None, name
        known = ", ".join(k for k in channels) or "(none set)"
        raise RuntimeError(
            f"telegram channel '{name}' is not in [telegram.channels] (or its id is "
            f"empty). Known channels: {known}. Fill it in the root config.toml, then "
            f"re-run.")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or str(tg.get("chat_id", ""))
    return chat_id, None, None


def _call(token: str, method: str, params: dict) -> dict:
    """POST a plain (non-file) Telegram API call."""
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        API.format(token=token, method=method), data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _multipart(fields: dict, file_field: str, path: Path) -> tuple[bytes, str]:
    """Encode fields + one file as multipart/form-data. Stdlib has no helper for
    this, so build the body by hand (same no-dependency stance as tiktok.py)."""
    boundary = "----clipcraft" + uuid.uuid4().hex
    crlf = b"\r\n"
    out: list[bytes] = []
    for key, val in fields.items():
        out += [
            f"--{boundary}".encode(), crlf,
            f'Content-Disposition: form-data; name="{key}"'.encode(), crlf, crlf,
            str(val).encode("utf-8"), crlf,
        ]
    ctype = mimetypes.guess_type(path.name)[0] or "video/mp4"
    out += [
        f"--{boundary}".encode(), crlf,
        (f'Content-Disposition: form-data; name="{file_field}"; '
         f'filename="{path.name}"').encode(), crlf,
        f"Content-Type: {ctype}".encode(), crlf, crlf,
        path.read_bytes(), crlf,
        f"--{boundary}--".encode(), crlf,
    ]
    return b"".join(out), boundary


def _send_video(token: str, chat_id: str, path: Path, caption: str,
                thread_id: int | None = None) -> dict:
    fields = {"chat_id": chat_id, "caption": caption[:CAPTION_LIMIT],
              "supports_streaming": "true"}
    if thread_id is not None:
        fields["message_thread_id"] = str(thread_id)
    body, boundary = _multipart(fields, "video", path)
    req = urllib.request.Request(
        API.format(token=token, method="sendVideo"), data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def _send_photo(token: str, chat_id: str, path: Path, caption: str,
                thread_id: int | None = None) -> dict:
    fields = {"chat_id": chat_id, "caption": caption[:CAPTION_LIMIT]}
    if thread_id is not None:
        fields["message_thread_id"] = str(thread_id)
    body, boundary = _multipart(fields, "photo", path)
    req = urllib.request.Request(
        API.format(token=token, method="sendPhoto"), data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def _caption_for(clip: dict, cfg: Config) -> str:
    """IG-ready caption: the full-context title first (the house-rule title already
    carries who/what/source), then hashtags."""
    tg = cfg.raw.get("telegram", {})
    up = cfg.raw.get("upload", {})
    title = (clip.get("title") or clip.get("hook") or "").strip()
    parts = [title] if title else []
    tags = tg.get("hashtags", up.get("hashtags", []))
    if tags:
        parts.append(" ".join(tags))
    footer = tg.get("caption_footer", "").strip()
    if footer:
        parts.append(footer)
    return "\n\n".join(parts)


def whoami(cfg: Config) -> int:
    """Print chat_id(s) seen in recent messages to the bot, so the user can grab
    theirs without a third-party bot. They must have messaged the bot first."""
    token, _ = _creds(cfg)
    resp = _call(token, "getUpdates", {})
    if not resp.get("ok"):
        print(f"[telegram] getUpdates failed: {resp}")
        return 1
    seen: dict[tuple, str] = {}
    for upd in resp.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if not chat:
            continue
        who = chat.get("title") or (
            f"{chat.get('first_name', '')} {chat.get('last_name', '')}".strip()
            or chat.get("username", ""))
        tid = msg.get("message_thread_id")
        tname = (msg.get("forum_topic_created") or {}).get("name", "")
        text = (msg.get("text") or msg.get("caption") or "").strip().replace("\n", " ")
        label = f"{who} ({chat.get('type')})"
        if tid is not None:
            label += f"  topic {tid}"
            if tname:
                label += f" '{tname}'"
            elif text:
                label += f'  last msg: "{text[:30]}"'
        seen[(str(chat["id"]), tid)] = label
    if not seen:
        print("[telegram] no messages found. Send your bot a message (or post inside "
              "a channel/topic where it's an admin), then run this again.")
        return 1
    print("[telegram] chats / channels / topics this bot can see:")
    for (cid, tid), who in seen.items():
        loc = f"chat_id {cid}" + (f", topic {tid}" if tid is not None else "")
        print(f"    {loc:32s} -> {who}")
    print("\nSeparate channels: put the chat_id under [telegram.channels].")
    print("One forum group + topics: set [telegram] group_id to the group's chat_id,")
    print("then per niche  pojok-x = { topic = <topic id> }  in [telegram.channels].")
    return 0


def send_batch(jobs, cfg: Config, manifest_path: Path, clips: list[dict],
               dry_run: bool = False, channel: str | None = None) -> int:
    try:
        chat_id, thread_id, dest_name = _resolve_dest(cfg, channel)
    except RuntimeError as exc:
        print(f"[telegram] {exc}")
        return 1
    label = f"channel '{dest_name}'" if dest_name else "default chat"
    if thread_id is not None:
        label += f" (topic {thread_id})"
    print(f"[telegram] {len(jobs)} clip(s) -> {label} (for Reels)"
          + (" [dry-run]" if dry_run else ""))
    for clip, _ in jobs:
        print(f"  clip {clip.get('clip')}: {(clip.get('title') or '').strip()}")
    if dry_run:
        print(f"[telegram] dry-run only; would send to {label} (chat_id={chat_id or 'UNSET'}).")
        return 0

    token, _ = _creds(cfg)
    if not chat_id:
        print("[telegram] no chat_id set. Run `telegram --whoami` to find yours, "
              "then add it to [telegram] chat_id, or route via [telegram.channels].")
        return 1

    sent = 0
    for i, (clip, path) in enumerate(jobs):
        p = Path(path)
        size = p.stat().st_size
        if size > MAX_BYTES:
            print(f"[telegram] clip {clip.get('clip')} is {size / 1e6:.1f} MB "
                  f"(> 50 MB Telegram cap); skipping. Grab it from: {p}")
            continue
        try:
            resp = _send_video(token, chat_id, p, _caption_for(clip, cfg), thread_id)
            if not resp.get("ok"):
                raise RuntimeError(resp.get("description", resp))
            clip["telegram"] = {"status": "sent",
                                "message_id": resp["result"].get("message_id")}
            sent += 1
            print(f"[telegram] clip {clip.get('clip')} -> sent to chat")
            # Also send the 9:16 title-card thumbnail as a Reel-cover reference.
            thumb = (clip.get("thumbnails") or {}).get("mobile") or clip.get("thumbnail")
            if thumb and Path(thumb).exists():
                try:
                    tr = _send_photo(token, chat_id, Path(thumb),
                                     f"Thumbnail — clip {clip.get('clip')}", thread_id)
                    if not tr.get("ok"):
                        raise RuntimeError(tr.get("description", tr))
                    print(f"[telegram] clip {clip.get('clip')} thumbnail -> sent")
                except Exception as exc:
                    print(f"[telegram] clip {clip.get('clip')} thumbnail FAILED: {exc}")
        except Exception as exc:
            print(f"[telegram] clip {clip.get('clip')} FAILED: {exc}")
        manifest_path.write_text(json.dumps(clips, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        if i < len(jobs) - 1:
            time.sleep(1)

    print(f"[telegram] done: {sent}/{len(jobs)} sent. Open Telegram on your phone -> "
          "save each video -> post to Instagram Reels (caption is ready to paste).")
    from . import tracker
    tracker.sync_safe(cfg)
    return 0 if sent else 1
