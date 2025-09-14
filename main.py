#!/usr/bin/env python
# main.py
"""
Telegram Video Leech Bot (Pyrogram v3+ compatible)
- Resolutions buttons (unique)
- /start, /help, /leech <url>
- Safe callback_data via token
- Download & upload progress updates (throttled)
- Remux to streamable MP4, fallback re-encode if needed
- Split > PART_MAX_BYTES into streamable parts using ffmpeg/time-based splitting
"""

import os
import sys
import uuid
import logging
import asyncio
import math
import time
import tempfile
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional

import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

# ---------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("leech-bot")

# ---------- Python version guard ----------
if sys.version_info < (3, 10):
    log.critical("Python 3.10+ required. Current version: %s", sys.version.split()[0])
    raise SystemExit("Python 3.10+ required.")

# -----------------------
# Configuration (env)
# -----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
TG_CHAT = int(os.getenv("TG_CHAT", "0"))  # admin/log chat
ALLOWED_USERS = [s.strip() for s in os.getenv("ALLOWED_USERS", "").split(",") if s.strip()]
PART_MAX_BYTES = int(float(os.getenv("PART_MAX_GB", "1.95")) * (1024 ** 3))
PROGRESS_UPDATE_INTERVAL = int(os.getenv("PROGRESS_UPDATE_INTERVAL", "9"))

if not BOT_TOKEN or not API_ID or not API_HASH or not TG_CHAT:
    raise RuntimeError("BOT_TOKEN, API_ID, API_HASH, TG_CHAT must be set")

# -----------------------
# Pyrogram client
# -----------------------
app = Client("leech-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# -----------------------
# Utilities
# -----------------------
def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return str(user_id) in ALLOWED_USERS or str(user_id) == str(TG_CHAT)

async def send_log(text: str):
    try:
        await app.send_message(TG_CHAT, text)
    except Exception:
        log.exception("Failed to send log to TG_CHAT")

def unique_formats_by_resolution(formats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_height: Dict[int, Dict[str, Any]] = {}
    for f in formats:
        if f.get("vcodec") == "none":
            continue
        height = f.get("height") or 0
        cur = by_height.get(height)
        score = (f.get("filesize") or 0) + int((f.get("tbr") or 0) * 1024)
        cur_score = 0
        if cur:
            cur_score = (cur.get("filesize") or 0) + int((cur.get("tbr") or 0) * 1024)
        if not cur or score > cur_score:
            by_height[height] = f
    return [by_height[h] for h in sorted(by_height.keys(), reverse=True)]

# -----------------------
# Progress notifier
# -----------------------
class ProgressNotifier:
    def __init__(self, edit_coroutine, min_interval: int = PROGRESS_UPDATE_INTERVAL):
        self.edit_coroutine = edit_coroutine
        self.min_interval = min_interval
        self._last_update = 0

    async def maybe_update(self, text: str, force: bool = False):
        now = time.monotonic()
        if force or (now - self._last_update >= self.min_interval):
            try:
                await self.edit_coroutine(text)
            except Exception:
                log.exception("Failed to update progress message")
            self._last_update = now

# -----------------------
# Subprocess helpers
# -----------------------
async def run_subprocess(cmd: List[str], cwd: Optional[str] = None, timeout: Optional[int] = None):
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, stdout.decode(errors="ignore"), stderr.decode(errors="ignore")

async def remux_to_streamable_mp4(src: Path, dst: Path) -> None:
    cmd = ["ffmpeg", "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dst)]
    code, out, err = await run_subprocess(cmd)
    if code != 0:
        raise RuntimeError(f"ffmpeg remux failed: {err}")

async def reencode_to_mp4(src: Path, dst: Path) -> None:
    cmd = ["ffmpeg", "-y", "-i", str(src), "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)]
    code, out, err = await run_subprocess(cmd)
    if code != 0:
        raise RuntimeError(f"ffmpeg re-encode failed: {err}")

async def get_duration_seconds(path: Path) -> float:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    code, out, err = await run_subprocess(cmd)
    if code != 0 or not out.strip():
        return 0.0
    try:
        return float(out.strip())
    except Exception:
        return 0.0

async def split_mp4_by_time(src: Path, out_dir: Path, max_bytes: int = PART_MAX_BYTES) -> List[Path]:
    size = src.stat().st_size
    if size <= max_bytes:
        return [src]
    duration = await get_duration_seconds(src)
    if duration <= 0:
        raise RuntimeError("Cannot determine duration for splitting")
    bytes_per_sec = size / duration
    seg_secs = max(5, int(math.floor(max_bytes / bytes_per_sec)))
    parts: List[Path] = []
    total_secs = int(math.ceil(duration))
    idx = 0
    for start in range(0, total_secs, seg_secs):
        idx += 1
        out_file = out_dir / f"{src.stem}.part{idx:02d}.mp4"
        cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", str(src), "-t", str(seg_secs),
               "-c", "copy", "-movflags", "+faststart", str(out_file)]
        code, out, err = await run_subprocess(cmd)
        if code != 0:
            raise RuntimeError(f"ffmpeg split failed at {start}s: {err}")
        parts.append(out_file)
    return parts

# -----------------------
# Session store
# -----------------------
SESSIONS: Dict[str, Dict[str, Any]] = {}

# -----------------------
# Command handlers
# -----------------------
@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        await message.reply("⛔ Access Denied")
        return
    await message.reply("✅ Bot running.\nSend a video URL or use /leech <url>.\nUse /help for details.")

@app.on_message(filters.command("help") & filters.private)
async def cmd_help(client: Client, message: Message):
    help_text = (
        "Usage:\n"
        "/start - check bot\n"
        "/help - this message\n"
        "/leech <url> - start leech (pick resolution)\n\n"
        "Or just send a video URL in private chat."
    )
    await message.reply(help_text)

@app.on_message(filters.command("leech") & filters.private)
async def cmd_leech(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        await message.reply("⛔ Access Denied")
        return
    if len(message.command) < 2:
        await message.reply("Usage: /leech <url>")
        return
    url = message.text.split(None, 1)[1].strip()
    await handle_incoming_url(client, message, url)

@app.on_message(filters.text & filters.private)
async def on_text(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        await message.reply("⛔ Access Denied")
        return
    url = message.text.strip()
    await handle_incoming_url(client, message, url)

async def handle_incoming_url(client: Client, message: Message, url: str):
    status = await message.reply_text("🔎 Fetching formats...")
    try:
        def fetch():
            opts = {"quiet": True, "skip_download": True, "no_warnings": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, fetch)

        formats = info.get("formats", []) or []
        video_formats = [f for f in formats if f.get("vcodec") != "none"]
        unique = unique_formats_by_resolution(video_formats)
        if not unique:
            await status.edit_text("No video formats found.")
            return

        token = uuid.uuid4().hex
        SESSIONS[token] = {"url": url, "info": info, "requested_by": message.from_user.id}

        keyboard = []
        for f in unique:
            height = f.get("height") or 0
            label = f"{height}p" if height > 0 else (f.get("format_note") or "auto")
            fmt_id = f.get("format_id")
            cb = f"LEECH:{token}:{fmt_id}"
            keyboard.append([InlineKeyboardButton(label, callback_data=cb)])
        keyboard.append([InlineKeyboardButton("Cancel ❌", callback_data=f"CANCEL:{token}")])

        await status.edit_text("Select resolution:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await status.edit_text(f"❌ Error: {e}")
        await send_log(f"Error preparing formats: {e}")

@app.on_callback_query()
async def on_callback(c: Client, cq: CallbackQuery):
    await cq.answer()
    user_id = cq.from_user.id
    if not is_allowed(user_id):
        await cq.answer("Access denied", show_alert=True)
        return

    data = cq.data or ""
    if data.startswith("CANCEL:"):
        token = data.split(":", 1)[1]
        await cq.message.edit_text("Cancelled.")
        SESSIONS.pop(token, None)
        return

    if not data.startswith("LEECH:"):
        await cq.answer()
        return

    _, token, fmt_id = data.split(":", 2)
    session = SESSIONS.get(token)
    if not session:
        await cq.answer("Session expired", show_alert=True)
        return

    url = session["url"]
    status_msg = await cq.message.reply_text(f"Queued: {url}\nFormat: {fmt_id}")
    asyncio.create_task(handle_leech_pipeline(c, user_id, cq.message.chat.id, url, fmt_id, status_msg.id))

# -----------------------
# Pipeline
# -----------------------
async def handle_leech_pipeline(client: Client, user_id: int, chat_id: int, url: str, format_id: str, status_message_id: int):
    tmpdir = Path(tempfile.mkdtemp(prefix="leech_"))
    try:
        await client.send_message(chat_id, f"⏬ Starting leech for format `{format_id}`")

        async def edit_status(text: str):
            try:
                await client.edit_message_text(chat_id=chat_id, message_id=status_message_id, text=text)
            except Exception:
                try:
                    await client.send_message(chat_id, text)
                except Exception:
                    pass

        notifier = ProgressNotifier(edit_status, PROGRESS_UPDATE_INTERVAL)
        last_hook_time = 0

        def ytdl_hook(d):
            nonlocal last_hook_time
            status_text = ""
            st = d.get("status")
            if st == "downloading":
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                speed = d.get("speed") or 0
                eta = d.get("eta") or 0
                percent = (downloaded / total * 100) if total else 0.0
                status_text = f"⬇️ Downloading: {d.get('filename','')}\n{percent:.2f}% • {downloaded//1024} KB / {total//1024 if total else 0} KB\nSpeed: {int(speed)//1024 if speed else 0} KB/s • ETA: {int(eta)}s"
            elif st == "finished":
                status_text = "⬇️ Download finished. Finalizing..."
            try:
                if time.monotonic() - last_hook_time >= PROGRESS_UPDATE_INTERVAL:
                    asyncio.get_event_loop().create_task(notifier.maybe_update(status_text, force=True))
                    last_hook_time = time.monotonic()
            except Exception:
                pass

        ydl_opts = {
            "format": format_id,
            "outtmpl": str(tmpdir / "%(title).200s.%(ext)s"),
            "noplaylist": True,
            "progress_hooks": [ytdl_hook],
            "quiet": True,
            "no_warnings": True,
        }

        loop = asyncio.get_event_loop()

        def run_ydl():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)

        try:
            info = await loop.run_in_executor(None, run_ydl)
        except Exception as e:
            await notifier.maybe_update(f"❌ Download failed: {e}", force=True)
            await send_log(f"Download failed: {url}: {e}")
            return

        files = sorted([p for p in tmpdir.iterdir() if p.is_file()], key=lambda p: p.stat().st_size, reverse=True)
        if not files:
            await notifier.maybe_update("❌ No file produced by yt-dlp.", force=True)
            return
        downloaded_file = files[0]
        await notifier.maybe_update(f"⬇️ Download complete: {downloaded_file.name}", force=True)

        remuxed = tmpdir / f"{downloaded_file.stem}.streamable.mp4"
        src_for_split = downloaded_file
        try:
            await notifier.maybe_update("🔧 Remuxing to streamable MP4...")
            await remux_to_streamable_mp4(downloaded_file, remuxed)
            src_for_split = remuxed
        except Exception:
            reencoded = tmpdir / f"{downloaded_file.stem}.reenc.mp4"
            await notifier.maybe_update("⚠️ Remux failed, re-encoding...")
            await reencode_to_mp4(downloaded_file, reencoded)
            src_for_split = reencoded

        size = src_for_split.stat().st_size
        if size > PART_MAX_BYTES:
            await notifier.maybe_update(f"✂️ Splitting into <= {PART_MAX_BYTES} bytes parts...")
            parts = await split_mp4_by_time(src_for_split, tmpdir, max_bytes=PART_MAX_BYTES)
        else:
            parts = [src_for_split]

        async def upload_with_progress(part_path: Path, part_index: int, total_parts: int):
            last_update = 0
            total_bytes = part_path.stat().st_size

            def progress_cb(current, total):
                nonlocal last_update
                now = time.monotonic()
                if now - last_update >= PROGRESS_UPDATE_INTERVAL:
                    percent = (current / total * 100) if total else 0.0
                    text = f"⬆️ Uploading part {part_index}/{total_parts}: {part_path.name}\n{percent:.2f}% • {current//1024} KB / {total//1024} KB"
                    asyncio.get_event_loop().create_task(notifier.maybe_update(text))
                    last_update = now

            await client.send_document(chat_id, str(part_path), caption=f"Part {part_index}/{total_parts} - {part_path.name}",
                                       progress=progress_cb, progress_args=(total_bytes,))

        total_parts = len(parts)
        for idx, p in enumerate(parts, start=1):
            await notifier.maybe_update(f"📤 Uploading part {idx}/{total_parts}: {p.name}", force=True)
            try:
                await upload_with_progress(p, idx, total_parts)
                await client.send_message(TG_CHAT, f"✔️ Uploaded part {idx}/{total_parts}: {p.name}")
            except Exception as e:
                await client.send_message(TG_CHAT, f"❌ Upload failed: {p.name} - {e}")
                await notifier.maybe_update(f"❌ Upload failed: {p.name} - {e}", force=True)
                return

        await notifier.maybe_update(f"✅ All parts uploaded successfully!", force=True)
    finally:
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass

# -----------------------
# Run bot
# -----------------------
if __name__ == "__main__":
    log.info("Starting bot...")
    app.run()
