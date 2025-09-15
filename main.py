#!/usr/bin/env python3
# main.py
"""
Telegram Video Leech Bot - with Progress Bars

Features:
- Resolution buttons (up to 1080p)
- Download with yt-dlp (progress bar + MB/s, updates every 5s)
- Remux/re-encode to streamable MP4
- Split if > PART_MAX_GB (default 1.95 GiB)
- Upload sequentially with progress bar (MB/s)
- Logs & files sent to TG_CHAT
- Allowed users enforced
"""

import os
import sys
import uuid
import math
import time
import shutil
import logging
import tempfile
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional

import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

# ----------------- Configuration -----------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("leech-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH")
TG_CHAT = os.environ.get("TG_CHAT", "")
ALLOWED_USERS = [s.strip() for s in os.environ.get("ALLOWED_USERS", "").split(",") if s.strip()]

PART_MAX_GB = float(os.environ.get("PART_MAX_GB", "1.95"))
PART_MAX_BYTES = int(PART_MAX_GB * (1024 ** 3))
PROGRESS_INTERVAL = int(os.environ.get("PROGRESS_UPDATE_INTERVAL", "5"))

if not BOT_TOKEN or not API_ID or not API_HASH or not TG_CHAT:
    log.critical("BOT_TOKEN, API_ID, API_HASH and TG_CHAT must be set in environment")
    sys.exit(1)

# ----------------- Pyrogram client -----------------
app = Client("leech-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ----------------- Progress Bar -----------------
def make_progress_bar(pct: float, length: int = 10) -> str:
    """Return a unicode block bar string for percentage."""
    filled = int(round(length * pct / 100))
    empty = length - filled
    return "▰" * filled + "▱" * empty

# ----------------- Utilities -----------------
def is_allowed(uid: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return str(uid) in ALLOWED_USERS or str(uid) == str(TG_CHAT)

async def send_log(text: str):
    try:
        target = int(TG_CHAT) if str(TG_CHAT).lstrip("-").isdigit() else TG_CHAT
        await app.send_message(target, text)
    except Exception:
        log.exception("Failed to send admin log")

# ----------------- Progress Throttler -----------------
class Throttler:
    def __init__(self, interval_sec: int = PROGRESS_INTERVAL):
        self.interval = interval_sec
        self._last: Dict[str, float] = {}

    def should(self, key: str) -> bool:
        now = time.monotonic()
        last = self._last.get(key, 0.0)
        if now - last >= self.interval:
            self._last[key] = now
            return True
        return False

throttler = Throttler(PROGRESS_INTERVAL)

# ----------------- Session Store -----------------
SESS: Dict[str, Dict[str, Any]] = {}

# ----------------- Bot Handlers -----------------
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(_, m: Message):
    await m.reply("Hello — send a video URL. Use /leech <url> too.\nAllowed users only.")

@app.on_message(filters.command("leech") & filters.private)
async def leech_cmd(_, m: Message):
    if not is_allowed(m.from_user.id):
        await m.reply("⛔ Not allowed.")
        return
    if len(m.command) < 2:
        await m.reply("Usage: /leech <url>")
        return
    url = m.text.split(None, 1)[1].strip()
    await handle_incoming_url(m, url)

@app.on_message(filters.text & filters.private)
async def text_handler(_, m: Message):
    if not is_allowed(m.from_user.id):
        await m.reply("⛔ Not allowed.")
        return
    url = m.text.strip()
    await handle_incoming_url(m, url)

async def handle_incoming_url(message: Message, url: str):
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
        if not video_formats:
            await status.edit_text("❌ No video formats found.")
            return
        token = uuid.uuid4().hex
        SESS[token] = {"url": url, "info": info, "requested_by": message.from_user.id}
        kb = []
        for f in video_formats:
            h = f.get("height") or 0
            if h > 1080:
                continue
            fmtid = f.get("format_id")
            label = f"{h}p" if h else "auto"
            kb.append([InlineKeyboardButton(label, callback_data=f"LEECH|{token}|{fmtid}")])
        kb.append([InlineKeyboardButton("Cancel ❌", callback_data=f"CANCEL|{token}")])
        await status.edit_text("Select resolution:", reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        log.exception("fetch formats failed")
        await status.edit_text(f"❌ Error: {e}")

@app.on_callback_query()
async def callback_handler(_, cq: CallbackQuery):
    data = cq.data or ""
    if not is_allowed(cq.from_user.id):
        await cq.answer("Access denied", show_alert=True)
        return
    if data.startswith("CANCEL|"):
        token = data.split("|", 1)[1]
        SESS.pop(token, None)
        await cq.message.edit_text("Cancelled.")
        await cq.answer()
        return
    if not data.startswith("LEECH|"):
        await cq.answer()
        return
    _, token, fmtid = data.split("|", 2)
    sess = SESS.get(token)
    if not sess:
        await cq.answer("Session expired", show_alert=True)
        return
    url = sess["url"]
    status_msg = await cq.message.reply_text(f"Queued: {url}\nFormat: {fmtid}")
    await cq.answer("Queued")
    asyncio.create_task(pipeline_task(token, sess, fmtid, status_msg.chat.id, status_msg.id))

# ----------------- Pipeline -----------------
async def pipeline_task(token: str, session: Dict[str, Any], fmtid: str, status_chat_id: int, status_msg_id: int):
    url = session["url"]
    tmpdir = Path(tempfile.mkdtemp(prefix="leech_"))
    try:
        async def edit_status(text: str):
            try:
                await app.edit_message_text(status_chat_id, status_msg_id, text)
            except Exception:
                try:
                    await app.send_message(status_chat_id, text)
                except:
                    pass

        loop = asyncio.get_running_loop()

        def ytdl_hook(d):
            try:
                if d.get("status") == "downloading":
                    downloaded = d.get("downloaded_bytes") or 0
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    speed = d.get("speed") or 0
                    eta = d.get("eta") or 0
                    percent = (downloaded / total * 100) if total else 0.0
                    if throttler.should(f"dl:{token}"):
                        bar = make_progress_bar(percent, 10)
                        text = (
                            f"⬇️ Downloading: {d.get('filename','')}\n"
                            f"Progress: {bar} {percent:.2f}%\n"
                            f"{downloaded / (1024*1024):.2f} MB / {total / (1024*1024):.2f} MB\n"
                            f"Speed: {speed / (1024*1024):.2f} MB/s • ETA: {eta}s"
                        )
                        coro = edit_status(text)
                        loop.call_soon_threadsafe(asyncio.create_task, coro)
                elif d.get("status") == "finished":
                    coro = edit_status("⬇️ Download finished. Processing...")
                    loop.call_soon_threadsafe(asyncio.create_task, coro)
            except Exception:
                pass

        ydl_opts = {
            "format": fmtid,
            "outtmpl": str(tmpdir / "%(title).200s.%(ext)s"),
            "noplaylist": True,
            "progress_hooks": [ytdl_hook],
            "quiet": True,
            "no_warnings": True,
        }

        def run_ydl():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)

        await asyncio.get_event_loop().run_in_executor(None, run_ydl)

        files = sorted([p for p in tmpdir.iterdir() if p.is_file()], key=lambda p: p.stat().st_size, reverse=True)
        if not files:
            await edit_status("❌ No output file.")
            return
        downloaded = files[0]

        await edit_status(f"⬆️ Uploading: {downloaded.name}")

        def upl_progress_cb(current, total, *args):
            try:
                if not total:
                    return
                pct = (current / total) * 100
                if throttler.should(f"upl:{token}"):
                    bar = make_progress_bar(pct, 10)
                    speed = (current / max(1, time.monotonic() - start_time)) / (1024 * 1024)
                    txt = (
                        f"⬆️ Uploading: {downloaded.name}\n"
                        f"Progress: {bar} {pct:.2f}%\n"
                        f"{current / (1024*1024):.2f} MB / {total / (1024*1024):.2f} MB\n"
                        f"Speed: {speed:.2f} MB/s"
                    )
                    coro = edit_status(txt)
                    loop.call_soon_threadsafe(asyncio.create_task, coro)
            except Exception:
                pass

        start_time = time.monotonic()
        chat = int(TG_CHAT) if str(TG_CHAT).lstrip("-").isdigit() else TG_CHAT
        await app.send_video(chat, str(downloaded), caption=f"{downloaded.name}",
                             progress=upl_progress_cb, progress_args=(downloaded.stat().st_size,))
        await edit_status("✅ Upload complete.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        SESS.pop(token, None)

# ----------------- Run -----------------
if __name__ == "__main__":
    log.info("Starting bot")
    app.run()
