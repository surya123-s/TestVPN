#!/usr/bin/env python
# main.py
"""
Telegram Video Leech Bot (Pyrogram + yt-dlp + ffmpeg)
Compatible with Windows runners and latest ffmpeg v8.
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
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

# ------------------------
# Requirements modules
# ------------------------
import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("leech-bot")

# ---------- Python version check ----------
if sys.version_info < (3, 10):
    log.critical("Python 3.10+ is required. Current version: %s", sys.version.split()[0])
    raise SystemExit("Python 3.10+ required. Update your Python.")

# -----------------------
# Configuration (env)
# -----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
TG_CHAT = int(os.getenv("TG_CHAT", "0"))
ALLOWED_USERS = [s.strip() for s in os.getenv("ALLOWED_USERS", "").split(",") if s.strip()]

PART_MAX_BYTES = int(float(os.getenv("PART_MAX_GB", "1.95")) * (1024 ** 3))
PROGRESS_UPDATE_INTERVAL = int(os.getenv("PROGRESS_UPDATE_INTERVAL", "9"))

# -----------------------
# ffmpeg detection (Windows compatible)
# -----------------------
def find_ffmpeg_bin() -> str:
    """Return path to ffmpeg binary, fallback to PATH if available"""
    # Common Chocolatey install path
    common_choco_path = Path("C:/ProgramData/chocolatey/lib/ffmpeg/tools/ffmpeg/bin/ffmpeg.exe")
    if common_choco_path.exists():
        return str(common_choco_path)
    # Fallback to PATH
    for exe_name in ["ffmpeg", "ffmpeg.exe"]:
        if shutil.which(exe_name):
            return exe_name
    raise RuntimeError("ffmpeg not found. Install ffmpeg and ensure it's in PATH or Chocolatey.")

def find_ffprobe_bin() -> str:
    """Return path to ffprobe binary, fallback to PATH if available"""
    common_choco_path = Path("C:/ProgramData/chocolatey/lib/ffmpeg/tools/ffmpeg/bin/ffprobe.exe")
    if common_choco_path.exists():
        return str(common_choco_path)
    for exe_name in ["ffprobe", "ffprobe.exe"]:
        if shutil.which(exe_name):
            return exe_name
    raise RuntimeError("ffprobe not found. Install ffmpeg and ensure it's in PATH or Chocolatey.")

FFMPEG_BIN = find_ffmpeg_bin()
FFPROBE_BIN = find_ffprobe_bin()

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
        cur_score = (cur.get("filesize") or 0) + int((cur.get("tbr") or 0) * 1024) if cur else 0
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
    cmd = [FFMPEG_BIN, "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dst)]
    code, out, err = await run_subprocess(cmd)
    if code != 0:
        raise RuntimeError(f"ffmpeg remux failed: {err}")

async def reencode_to_mp4(src: Path, dst: Path) -> None:
    cmd = [FFMPEG_BIN, "-y", "-i", str(src), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)]
    code, out, err = await run_subprocess(cmd)
    if code != 0:
        raise RuntimeError(f"ffmpeg re-encode failed: {err}")

async def get_duration_seconds(path: Path) -> float:
    cmd = [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
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
        raise RuntimeError("Cannot determine duration for splitting; ffprobe failed.")

    bytes_per_sec = size / duration
    seg_secs = max(5, int(math.floor(max_bytes / bytes_per_sec)))
    if seg_secs <= 0:
        seg_secs = 10

    parts: List[Path] = []
    total_secs = int(math.ceil(duration))
    idx = 0
    for start in range(0, total_secs, seg_secs):
        idx += 1
        out_file = out_dir / f"{src.stem}.part{idx:02d}.mp4"
        cmd = [FFMPEG_BIN, "-y", "-ss", str(start), "-i", str(src), "-t", str(seg_secs),
               "-c", "copy", "-movflags", "+faststart", str(out_file)]
        code, out, err = await run_subprocess(cmd)
        if code != 0:
            raise RuntimeError(f"ffmpeg split failed at {start}s: {err}")
        parts.append(out_file)

    for p in parts:
        if p.stat().st_size > max_bytes + 1024 * 1024:
            raise RuntimeError(f"Part too large after split: {p} ({p.stat().st_size})")
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
        "/leech <url> - start leech\n\n"
        "Or just send a video URL in private chat and pick resolution from buttons.\n"
        "Only allowed users can use the bot (ALLOWED_USERS)."
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

# -----------------------
# Handle incoming URL
# -----------------------
async def handle_incoming_url(client: Client, message: Message, url: str):
    status = await message.reply_text("🔎 Fetching available formats, please wait...")
    try:
        def fetch():
            opts = {"quiet": True, "skip_download": True, "no_warnings": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        loop = asyncio.get_event_loop()
        try:
            info = await loop.run_in_executor(None, fetch)
        except Exception as e:
            await status.edit_text(f"❌ Failed to fetch formats: {e}")
            await send_log(f"Format fetch failed for {url}: {e}")
            return

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
        log.exception("Error handling URL")
        await status.edit_text(f"❌ Error: {e}")
        await send_log(f"Error handling URL {url}: {e}")

# -----------------------
# Callback query handler
# -----------------------
@app.on_callback_query()
async def on_callback(client: Client, cq: CallbackQuery):
    data = cq.data
    if data.startswith("LEECH:"):
        _, token, fmt_id = data.split(":")
        session = SESSIONS.get(token)
        if not session:
            await cq.answer("Session expired", show_alert=True)
            return
        await cq.answer("Downloading...", show_alert=False)
        await process_download(cq, session, fmt_id)
    elif data.startswith("CANCEL:"):
        token = data.split(":")[1]
        SESSIONS.pop(token, None)
        await cq.message.edit_text("❌ Operation canceled.")

# -----------------------
# Download handler
# -----------------------
async def process_download(cq: CallbackQuery, session: Dict[str, Any], fmt_id: str):
    info = session["info"]
    url = session["url"]
    user_id = session["requested_by"]
    temp_dir = Path(tempfile.mkdtemp(prefix="leech-"))
    status_msg = await cq.message.edit_text("⬇️ Downloading video...")

    try:
        opts = {
            "format": fmt_id,
            "outtmpl": str(temp_dir / "%(title)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "progress_hooks": [],
            "merge_output_format": "mp4",
            "ffmpeg_location": str(Path(FFMPEG_BIN).parent),
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            def progress_hook(d):
                if d.get("status") == "downloading":
                    asyncio.create_task(status_msg.edit_text(
                        f"⬇️ Downloading {d.get('filename','')} - {d.get('_percent_str','0%')} ({d.get('_eta_str','')})"
                    ))
            opts["progress_hooks"].append(progress_hook)
            ydl.download([url])

        # After download, remux/reencode if necessary
        files = list(temp_dir.glob("*.mp4"))
        if not files:
            await status_msg.edit_text("❌ No downloaded files found")
            return
        final_file = files[0]
        final_file_size = final_file.stat().st_size
        if final_file_size > PART_MAX_BYTES:
            parts = await split_mp4_by_time(final_file, temp_dir)
            await status_msg.edit_text(f"✅ Downloaded and split into {len(parts)} parts")
        else:
            await status_msg.edit_text(f"✅ Downloaded: {final_file.name} ({final_file_size / (1024**2):.2f} MB)")

    except Exception as e:
        log.exception("Download failed")
        await status_msg.edit_text(f"❌ Download failed: {e}")
        await send_log(f"Download failed for {url}: {e}")
    finally:
        # Cleanup temp dir
        shutil.rmtree(temp_dir, ignore_errors=True)

# -----------------------
# Main entry
# -----------------------
if __name__ == "__main__":
    log.info("Starting Telegram Leech Bot...")
    app.run()
