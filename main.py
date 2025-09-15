#!/usr/bin/env python3
# main.py
"""
Telegram Video Leech Bot - with Progress Bars & Splitting

Features implemented:
- Unique resolution buttons (up to 1080p)
- Download via yt-dlp, progress bar + MB/s, updates throttled by PROGRESS_INTERVAL
- Remux to streamable mp4, fallback re-encode to H264/AAC
- Split > PART_MAX_GB into streamable mp4 parts (time-based) so each part <= PART_MAX_BYTES
- Upload parts sequentially using send_video (mp4) with throttled upload progress updates
- All admin/log messages to TG_CHAT
- Allowed users enforced via ALLOWED_USERS env variable (comma separated IDs)
- Safe scheduling of threaded callbacks into the asyncio loop
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
import subprocess
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
PROGRESS_INTERVAL = int(os.environ.get("PROGRESS_UPDATE_INTERVAL", "5")) # seconds

# sanity checks
if not BOT_TOKEN or not API_ID or not API_HASH or not TG_CHAT:
    log.critical("BOT_TOKEN, API_ID, API_HASH and TG_CHAT must be set in environment")
    sys.exit(1)

# ----------------- Pyrogram client -----------------
app = Client("leech-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ----------------- ffmpeg / ffprobe discovery -----------------
def _which_any(names: List[str]) -> Optional[str]:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None

def find_ffmpeg() -> str:
    p = _which_any(["ffmpeg", "ffmpeg.exe"])
    if p:
        return p
    choco_path = Path("C:/ProgramData/chocolatey/lib/ffmpeg/tools/ffmpeg/bin/ffmpeg.exe")
    if choco_path.exists():
        return str(choco_path)
    raise RuntimeError("ffmpeg not found. Install ffmpeg or add it to PATH")

def find_ffprobe() -> str:
    p = _which_any(["ffprobe", "ffprobe.exe"])
    if p:
        return p
    choco_path = Path("C:/ProgramData/chocolatey/lib/ffmpeg/tools/ffmpeg/bin/ffprobe.exe")
    if choco_path.exists():
        return str(choco_path)
    raise RuntimeError("ffprobe not found. Install ffprobe or add it to PATH")

try:
    FFMPEG = find_ffmpeg()
    FFPROBE = find_ffprobe()
    log.info("ffmpeg: %s, ffprobe: %s", FFMPEG, FFPROBE)
except Exception as e:
    log.warning("ffmpeg/ffprobe not found: %s - some operations will fail if ffmpeg is missing", e)
    FFMPEG = "ffmpeg"
    FFPROBE = "ffprobe"

# ----------------- Utilities -----------------
def is_allowed(uid: int) -> bool:
    # if ALLOWED_USERS empty -> allow everyone
    if not ALLOWED_USERS:
        return True
    return str(uid) in ALLOWED_USERS or str(uid) == str(TG_CHAT)

async def send_log(text: str):
    try:
        target = int(TG_CHAT) if str(TG_CHAT).lstrip("-").isdigit() else TG_CHAT
        await app.send_message(target, text)
    except Exception:
        log.exception("Failed to send admin log")

def make_progress_bar(pct: float, length: int = 10) -> str:
    filled = int(round(length * pct / 100))
    filled = max(0, min(length, filled))
    empty = length - filled
    return "▰" * filled + "▱" * empty

def human_size_bytes(n: int) -> str:
    if not n:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    value = float(n)
    while value >= 1024 and i < len(units)-1:
        value /= 1024.0
        i += 1
    return f"{value:.2f} {units[i]}"

# ----------------- Throttler -----------------
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

# ----------------- Split / remux helpers -----------------
async def run_cmd(cmd: List[str], cwd: Optional[str] = None, timeout: Optional[int] = None):
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")

async def remux_to_streamable_mp4(src: Path, dst: Path):
    cmd = [FFMPEG, "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dst)]
    code, out, err = await run_cmd(cmd)
    if code != 0:
        raise RuntimeError(f"ffmpeg remux failed: {err}")

async def reencode_to_h264_aac(src: Path, dst: Path):
    cmd = [FFMPEG, "-y", "-i", str(src),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart",
           str(dst)]
    code, out, err = await run_cmd(cmd)
    if code != 0:
        raise RuntimeError(f"ffmpeg re-encode failed: {err}")

async def ffprobe_duration_seconds(path: Path) -> float:
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    code, out, err = await run_cmd(cmd)
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
    duration = await ffprobe_duration_seconds(src)
    if duration <= 0:
        raise RuntimeError("Cannot determine duration for splitting (ffprobe returned 0)")
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
        cmd = [FFMPEG, "-y", "-ss", str(start), "-i", str(src), "-t", str(seg_secs), "-c", "copy", "-movflags", "+faststart", str(out_file)]
        code, out, err = await run_cmd(cmd)
        if code != 0:
            raise RuntimeError(f"ffmpeg split failed at {start}s: {err}")
        parts.append(out_file)
    # sanity
    for p in parts:
        if p.stat().st_size > max_bytes + 1024 * 1024:
            raise RuntimeError(f"Part too large after split: {p} ({p.stat().st_size})")
    return parts

# ----------------- Session store -----------------
SESS: Dict[str, Dict[str, Any]] = {} # token -> {url, info, requested_by}

# ----------------- Bot handlers -----------------
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(c: Client, m: Message):
    await m.reply("Hello — send a video URL privately or use /leech <url>.\nAllowed users only when configured.")

@app.on_message(filters.command("help") & filters.private)
async def help_cmd(c: Client, m: Message):
    await m.reply("Send a video URL or use `/leech <url>`. Choose resolution button (up to 1080p).")

@app.on_message(filters.command("leech") & filters.private)
async def leech_cmd(c: Client, m: Message):
    if not is_allowed(m.from_user.id):
        await m.reply("⛔ You are not allowed to use this bot.")
        return
    if len(m.command) < 2:
        await m.reply("Usage: /leech <url>")
        return
    url = m.text.split(None, 1)[1].strip()
    await handle_incoming_url(m, url)

@app.on_message(filters.text & filters.private)
async def text_handler(c: Client, m: Message):
    if not is_allowed(m.from_user.id):
        await m.reply("⛔ You are not allowed to use this bot.")
        return
    url = m.text.strip()
    await handle_incoming_url(m, url)

async def handle_incoming_url(message: Message, url: str):
    status = await message.reply_text("🔎 Fetching formats, please wait...")
    try:
        def fetch():
            opts = {"quiet": True, "skip_download": True, "no_warnings": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, fetch)
        formats = info.get("formats", []) or []
        video_formats = [f for f in formats if f.get("vcodec") != "none"]
        unique = {}
        # build unique by height, prefer best tbr/filesize
        for f in video_formats:
            h = f.get("height") or 0
            if h > 1080:
                continue
            cur = unique.get(h)
            score = (f.get("filesize") or 0) + int((f.get("tbr") or 0) * 1024)
            cur_score = 0
            if cur:
                cur_score = (cur.get("filesize") or 0) + int((cur.get("tbr") or 0) * 1024)
            if not cur or score > cur_score:
                unique[h] = f
        choices = [unique[h] for h in sorted(unique.keys(), reverse=True)]
        if not choices:
            await status.edit_text("❌ No video formats found.")
            return
        token = uuid.uuid4().hex
        SESS[token] = {"url": url, "info": info, "requested_by": message.from_user.id}
        kb = []
        for f in choices:
            h = f.get("height") or 0
            label = f"{h}p" if h else "auto"
            fmtid = f.get("format_id")
            kb.append([InlineKeyboardButton(label, callback_data=f"LEECH|{token}|{fmtid}")])
        kb.append([InlineKeyboardButton("Cancel ❌", callback_data=f"CANCEL|{token}")])
        await status.edit_text("Select resolution:", reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        log.exception("fetch formats failed")
        await status.edit_text(f"❌ Error fetching formats: {e}")
        await send_log(f"Error fetching formats for {url}: {e}")

@app.on_callback_query()
async def callback_handler(c: Client, cq: CallbackQuery):
    data = cq.data or ""
    user_id = cq.from_user.id
    if not is_allowed(user_id):
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
    # kickoff pipeline in background
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
                except Exception:
                    log.exception("Failed to send/edit status")

        # running loop used for scheduling from threads
        loop = asyncio.get_running_loop()

        # --- yt-dlp hook (called from executor thread) ---
        def ytdl_hook(d):
            try:
                st = d.get("status")
                if st == "downloading":
                    downloaded = d.get("downloaded_bytes") or 0
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    speed = d.get("speed") or 0
                    eta = d.get("eta") or 0
                    percent = (downloaded / total * 100) if total else 0.0
                    key = f"dl:{token}"
                    if throttler.should(key):
                        bar = make_progress_bar(percent, 10)
                        text = (
                            f"⬇️ Downloading: {d.get('filename','')}\n"
                            f"Progress: {bar} {percent:.2f}%\n"
                            f"{downloaded / (1024*1024):.2f} MB / {total / (1024*1024):.2f} MB\n"
                            f"Speed: {speed / (1024*1024):.2f} MB/s • ETA: {int(eta)}s"
                        )
                        coro = edit_status(text)
                        loop.call_soon_threadsafe(asyncio.create_task, coro)
                elif st == "finished":
                    key = f"dl:{token}:finished"
                    if throttler.should(key):
                        coro = edit_status("⬇️ Download finished. Finalizing...")
                        loop.call_soon_threadsafe(asyncio.create_task, coro)
            except Exception:
                log.exception("ytdl hook error")

        outtmpl = str(tmpdir / "%(title).200s.%(ext)s")
        ydl_opts = {
            "format": fmtid,
            "outtmpl": outtmpl,
            "noplaylist": True,
            "progress_hooks": [ytdl_hook],
            "quiet": True,
            "no_warnings": True,
            # ffmpeg location optional: use parent folder of FFMPEG if available
            "ffmpeg_location": str(Path(FFMPEG).parent) if FFMPEG else None,
        }

        def run_ydl():
            try:
                with yt_dlp.YoutubeDL({k: v for k, v in ydl_opts.items() if v is not None}) as ydl:
                    return ydl.extract_info(url, download=True)
            except Exception as e:
                # re-raise to executor
                raise

        try:
            info = await asyncio.get_event_loop().run_in_executor(None, run_ydl)
        except Exception as e:
            log.exception("yt-dlp failed")
            await edit_status(f"❌ yt-dlp failed: {e}")
            await send_log(f"yt-dlp failed for {url}: {e}")
            return

        # find downloaded files
        files = sorted([p for p in tmpdir.iterdir() if p.is_file()], key=lambda p: p.stat().st_size, reverse=True)
        if not files:
            await edit_status("❌ Download produced no files.")
            await send_log(f"No output file for {url}")
            return
        downloaded = files[0]
        await edit_status(f"⬇️ Download complete: {downloaded.name}")

        # remux or re-encode to streamable mp4
        remuxed = tmpdir / f"{downloaded.stem}.streamable.mp4"
        src_for_split = downloaded
        try:
            await edit_status("🔧 Remuxing to streamable MP4...")
            await remux_to_streamable_mp4(downloaded, remuxed)
            src_for_split = remuxed
        except Exception:
            await edit_status("⚠️ Remux failed — attempting re-encode (slower)...")
            try:
                reencoded = tmpdir / f"{downloaded.stem}.reenc.mp4"
                await reencode_to_h264_aac(downloaded, reencoded)
                src_for_split = reencoded
            except Exception as e:
                log.exception("re-encode failed")
                await edit_status(f"❌ Remux & re-encode both failed: {e}")
                await send_log(f"Remux/reencode failed for {downloaded}: {e}")
                return

        # split if needed
        size = src_for_split.stat().st_size
        if size > PART_MAX_BYTES:
            await edit_status(f"✂️ Splitting into parts (<= {PART_MAX_BYTES} bytes ~ {PART_MAX_GB} GiB)...")
            try:
                parts = await split_mp4_by_time(src_for_split, tmpdir, max_bytes=PART_MAX_BYTES)
            except Exception as e:
                log.exception("split failed")
                await edit_status(f"❌ Splitting failed: {e}")
                await send_log(f"Splitting failed for {src_for_split}: {e}")
                return
        else:
            parts = [src_for_split]

        # upload parts sequentially with progress
        total_parts = len(parts)
        chat = int(TG_CHAT) if str(TG_CHAT).lstrip("-").isdigit() else TG_CHAT

        for idx, part in enumerate(parts, start=1):
            await edit_status(f"📤 Uploading part {idx}/{total_parts}: {part.name}")
            # track start time for speed estimate
            start_time = time.monotonic()
            last_key = f"upl:{token}:{part.name}"

            def upl_progress_cb(current, total, *args):
                try:
                    if not total:
                        return
                    pct = (current / total) * 100
                    if throttler.should(last_key):
                        bar = make_progress_bar(pct, 10)
                        elapsed = max(0.0001, time.monotonic() - start_time)
                        # average speed since start
                        speed_mb_s = (current / elapsed) / (1024 * 1024)
                        txt = (
                            f"⬆️ Uploading part {idx}/{total_parts}: {part.name}\n"
                            f"Progress: {bar} {pct:.2f}%\n"
                            f"{current / (1024*1024):.2f} MB / {total / (1024*1024):.2f} MB\n"
                            f"Speed: {speed_mb_s:.2f} MB/s"
                        )
                        coro = edit_status(txt)
                        loop.call_soon_threadsafe(asyncio.create_task, coro)
                except Exception:
                    log.exception("upload progress cb error")

            try:
                if part.suffix.lower() == ".mp4":
                    await app.send_video(chat, str(part), caption=f"Part {idx}/{total_parts} - {part.name}",
                                         progress=upl_progress_cb, progress_args=(part.stat().st_size,))
                else:
                    await app.send_document(chat, str(part), caption=f"Part {idx}/{total_parts} - {part.name}",
                                            progress=upl_progress_cb, progress_args=(part.stat().st_size,))
                # notify log chat
                await send_log(f"✔️ Uploaded part {idx}/{total_parts}: {part.name}")
            except Exception as e:
                log.exception("upload failed")
                await edit_status(f"❌ Upload failed for {part.name}: {e}")
                await send_log(f"Upload failed for {part.name}: {e}")
                return

        await edit_status(f"✅ All done. Uploaded {len(parts)} file(s).")
        await send_log(f"Completed leech: {url} -> {len(parts)} parts")
    finally:
        # cleanup
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
        SESS.pop(token, None)

# ----------------- Run -----------------
if __name__ == "__main__":
    log.info("Starting leech-bot (PID %s)", os.getpid())
    app.run()