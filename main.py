#!/usr/bin/env python3
# main.py
"""
Telegram Video Leech Bot - full, Windows/GHA compatible

Features:
- Presents unique resolution buttons (up to 1080p)
- Download selected format with yt-dlp (progress displayed, throttled 5s)
- Remux to streamable MP4 (-movflags +faststart); fallback re-encode to H.264/AAC MP4
- If final file > PART_MAX_GB (1.95 GiB) split by time using ffmpeg (streamable parts)
- Upload parts sequentially as send_video (mp4) with upload progress (throttled 5s)
- Logs and final files sent to TG_CHAT (env)
- Allowed users enforced via ALLOWED_USERS (comma-separated IDs)
- Robust scheduling of progress updates from threaded callbacks
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

# ----------------- Configuration (env) -----------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("leech-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH")
TG_CHAT = os.environ.get("TG_CHAT", "")  # integer id (as string) or '@username'
ALLOWED_USERS = [s.strip() for s in os.environ.get("ALLOWED_USERS", "").split(",") if s.strip()]

# maximum part bytes (default 1.95 GiB)
PART_MAX_GB = float(os.environ.get("PART_MAX_GB", "1.95"))
PART_MAX_BYTES = int(PART_MAX_GB * (1024 ** 3))

# how often to update progress (seconds)
PROGRESS_INTERVAL = int(os.environ.get("PROGRESS_UPDATE_INTERVAL", "5"))

if not BOT_TOKEN or not API_ID or not API_HASH or not TG_CHAT:
    log.critical("BOT_TOKEN, API_ID, API_HASH and TG_CHAT must be set in environment")
    sys.exit(1)

# ----------------- Pyrogram client -----------------
app = Client("leech-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ----------------- ffmpeg / ffprobe detection -----------------
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
    raise RuntimeError("ffmpeg not found. Install ffmpeg or add to PATH.")

def find_ffprobe() -> str:
    p = _which_any(["ffprobe", "ffprobe.exe"])
    if p:
        return p
    choco_path = Path("C:/ProgramData/chocolatey/lib/ffmpeg/tools/ffmpeg/bin/ffprobe.exe")
    if choco_path.exists():
        return str(choco_path)
    raise RuntimeError("ffprobe not found. Install ffmpeg or add to PATH.")

FFMPEG = find_ffmpeg()
FFPROBE = find_ffprobe()
log.info("ffmpeg: %s, ffprobe: %s", FFMPEG, FFPROBE)

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

def unique_formats_by_resolution(formats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    From yt-dlp format list take single best candidate per numeric height (<=1080).
    """
    by_h: Dict[int, Dict[str, Any]] = {}
    for f in formats:
        if f.get("vcodec") == "none":
            continue
        h = f.get("height") or 0
        if h > 1080:
            continue
        cur = by_h.get(h)
        score = (f.get("filesize") or 0) + int((f.get("tbr") or 0) * 1024)
        cur_score = 0
        if cur:
            cur_score = (cur.get("filesize") or 0) + int((cur.get("tbr") or 0) * 1024)
        if not cur or score > cur_score:
            by_h[h] = f
    # return descending order (bigger first)
    return [by_h[h] for h in sorted(by_h.keys(), reverse=True)]

# ----------------- Subprocess helpers (async) -----------------
async def run_cmd(cmd: List[str], cwd: Optional[str] = None, timeout: Optional[int] = None):
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")

async def remux_to_streamable(src: Path, dst: Path):
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
        raise RuntimeError(f"ffmpeg reencode failed: {err}")

async def ffprobe_duration(path: Path) -> float:
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    code, out, err = await run_cmd(cmd)
    if code != 0 or not out.strip():
        return 0.0
    try:
        return float(out.strip())
    except Exception:
        return 0.0

async def split_mp4_by_time(src: Path, dest_dir: Path, max_bytes: int = PART_MAX_BYTES) -> List[Path]:
    size = src.stat().st_size
    if size <= max_bytes:
        return [src]
    duration = await ffprobe_duration(src)
    if duration <= 0:
        raise RuntimeError("ffprobe couldn't get duration for splitting")
    bytes_per_sec = size / duration
    seg_secs = max(5, int(math.floor(max_bytes / bytes_per_sec)))
    if seg_secs <= 0:
        seg_secs = 10
    parts: List[Path] = []
    total_secs = int(math.ceil(duration))
    idx = 0
    for start in range(0, total_secs, seg_secs):
        idx += 1
        out_file = dest_dir / f"{src.stem}.part{idx:02d}.mp4"
        cmd = [FFMPEG, "-y", "-ss", str(start), "-i", str(src), "-t", str(seg_secs), "-c", "copy", "-movflags", "+faststart", str(out_file)]
        code, out, err = await run_cmd(cmd)
        if code != 0:
            raise RuntimeError(f"ffmpeg split failed at {start}s: {err}")
        parts.append(out_file)
    # sanity: ensure each part <= max + slack
    for p in parts:
        if p.stat().st_size > max_bytes + 1024 * 1024:
            raise RuntimeError(f"Part too large after split: {p} ({p.stat().st_size})")
    return parts

# ----------------- Progress throttler (shared) -----------------
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

# ----------------- Session store -----------------
SESS: Dict[str, Dict[str, Any]] = {}  # token -> info

# ----------------- Bot handlers -----------------
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(_, m: Message):
    await m.reply("Hello — send a video URL (private). Use /leech <url> too.\nOnly allowed users can use the bot (if configured).")

@app.on_message(filters.command("help") & filters.private)
async def help_cmd(_, m: Message):
    await m.reply("Send URL or /leech <url>. Then choose a resolution button (up to 1080p).")

@app.on_message(filters.command("leech") & filters.private)
async def leech_cmd(_, m: Message):
    if not is_allowed(m.from_user.id):
        await m.reply("⛔ You are not allowed to use this bot.")
        return
    if len(m.command) < 2:
        await m.reply("Usage: /leech <url>")
        return
    url = m.text.split(None, 1)[1].strip()
    await handle_incoming_url(m, url)

@app.on_message(filters.text & filters.private)
async def text_handler(_, m: Message):
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
        unique = unique_formats_by_resolution(video_formats)
        if not unique:
            await status.edit_text("❌ No video formats found.")
            return
        token = uuid.uuid4().hex
        SESS[token] = {"url": url, "info": info, "requested_by": message.from_user.id}
        # build keyboard: one button per resolution
        kb = []
        for f in unique:
            h = f.get("height") or 0
            label = f"{h}p" if h > 0 else (f.get("format_note") or "auto")
            fmtid = f.get("format_id")
            kb.append([InlineKeyboardButton(label, callback_data=f"LEECH|{token}|{fmtid}")])
        kb.append([InlineKeyboardButton("Cancel ❌", callback_data=f"CANCEL|{token}")])
        await status.edit_text("Select resolution:", reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        log.exception("fetch formats failed")
        await status.edit_text(f"❌ Error fetching formats: {e}")
        await send_log(f"Error fetching formats for {url}: {e}")

@app.on_callback_query()
async def callback_handler(_, cq: CallbackQuery):
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
    # reply queued and capture status message id
    status_msg = await cq.message.reply_text(f"Queued: {url}\nFormat: {fmtid}")
    await cq.answer("Queued")
    # run pipeline in background
    asyncio.create_task(pipeline_task(token, sess, fmtid, status_msg.chat.id, status_msg.id))

# ----------------- Pipeline -----------------
async def pipeline_task(token: str, session: Dict[str, Any], fmtid: str, status_chat_id: int, status_msg_id: int):
    url = session["url"]
    requested_by = session.get("requested_by")
    tmpdir = Path(tempfile.mkdtemp(prefix="leech_"))
    try:
        # helper to edit status (coroutine)
        async def edit_status(text: str):
            try:
                await app.edit_message_text(chat_id=status_chat_id, message_id=status_msg_id, text=text)
            except Exception:
                try:
                    await app.send_message(status_chat_id, text)
                except Exception:
                    log.exception("Both edit_message_text and send_message failed")

        # prepare loop to schedule from hooks running in threads
        loop = asyncio.get_running_loop()

        # yt-dlp progress hook (runs in executor thread) -> schedule onto event loop safely
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
                        text = f"⬇️ Downloading: {d.get('filename','')}\n{percent:.2f}% • {downloaded//1024} KB / {total//1024 if total else 0} KB\nSpeed: {int(speed)//1024 if speed else 0} KB/s • ETA: {int(eta)}s"
                        # schedule coroutine on main loop thread
                        coro = edit_status(text)
                        loop.call_soon_threadsafe(asyncio.create_task, coro)
                elif st == "finished":
                    key = f"dl:{token}:finished"
                    if throttler.should(key):
                        coro = edit_status("⬇️ Download finished. Finalizing...")
                        loop.call_soon_threadsafe(asyncio.create_task, coro)
            except Exception:
                # avoid raising inside executor thread
                log.exception("ytdl hook error")

        # run yt-dlp in executor
        outtmpl = str(tmpdir / "%(title).200s.%(ext)s")
        ydl_opts = {
            "format": fmtid,
            "outtmpl": outtmpl,
            "noplaylist": True,
            "progress_hooks": [ytdl_hook],
            "quiet": True,
            "no_warnings": True,
            "ffmpeg_location": str(Path(FFMPEG).parent),
        }

        def run_ydl():
            with yt_dlp.YoutubeDL({k: v for k, v in ydl_opts.items() if v is not None}) as ydl:
                return ydl.extract_info(url, download=True)

        try:
            info = await asyncio.get_event_loop().run_in_executor(None, run_ydl)
        except Exception as e:
            log.exception("yt-dlp failed")
            await edit_status(f"❌ yt-dlp failed: {e}")
            await send_log(f"yt-dlp failed for {url}: {e}")
            return

        # find downloaded file(s)
        files = sorted([p for p in tmpdir.iterdir() if p.is_file()], key=lambda p: p.stat().st_size, reverse=True)
        if not files:
            await edit_status("❌ Download produced no files.")
            await send_log(f"No output file for {url}")
            return
        downloaded = files[0]
        await edit_status(f"⬇️ Download complete: {downloaded.name}")

        # ensure streamable mp4
        remuxed = tmpdir / f"{downloaded.stem}.streamable.mp4"
        src_for_split = downloaded
        try:
            await edit_status("🔧 Remuxing to streamable MP4...")
            await remux_to_streamable(downloaded, remuxed)
            src_for_split = remuxed
        except Exception:
            await edit_status("⚠️ Remux failed, re-encoding (slower)...")
            try:
                reenc = tmpdir / f"{downloaded.stem}.reenc.mp4"
                await reencode_to_h264_aac(downloaded, reenc)
                src_for_split = reenc
            except Exception as e:
                log.exception("re-encode failed")
                await edit_status(f"❌ Remux & re-encode both failed: {e}")
                await send_log(f"Remux/reencode failed for {downloaded}: {e}")
                return

        # split if bigger than allowed
        size = src_for_split.stat().st_size
        if size > PART_MAX_BYTES:
            await edit_status(f"✂️ Splitting into parts (<= {PART_MAX_BYTES} bytes)...")
            try:
                parts = await split_mp4_by_time(src_for_split, tmpdir, max_bytes=PART_MAX_BYTES)
            except Exception as e:
                log.exception("split failed")
                await edit_status(f"❌ Splitting failed: {e}")
                await send_log(f"Splitting failed for {src_for_split}: {e}")
                return
        else:
            parts = [src_for_split]

        # upload parts sequentially
        total_parts = len(parts)
        for idx, part in enumerate(parts, start=1):
            await edit_status(f"📤 Uploading part {idx}/{total_parts}: {part.name}")
            # make progress callback — Pyrogram may call it in a different thread — schedule edits via loop.call_soon_threadsafe
            last_key = f"upl:{token}:{part.name}"

            def upl_progress_cb(current, total, *args):
                try:
                    if not total:
                        return
                    if throttler.should(last_key):
                        pct = (current / total) * 100
                        txt = f"⬆️ Uploading part {idx}/{total_parts}: {part.name}\n{pct:.2f}% • {current//1024} KB / {total//1024} KB"
                        coro = edit_status(txt)
                        loop.call_soon_threadsafe(asyncio.create_task, coro)
                except Exception:
                    log.exception("upload progress cb error")

            # try to use send_video for mp4 (better for streaming/playback)
            try:
                chat = int(TG_CHAT) if str(TG_CHAT).lstrip("-").isdigit() else TG_CHAT
                if part.suffix.lower() == ".mp4":
                    await app.send_video(chat, str(part), caption=f"Part {idx}/{total_parts} - {part.name}",
                                         progress=upl_progress_cb, progress_args=(part.stat().st_size,))
                else:
                    await app.send_document(chat, str(part), caption=f"Part {idx}/{total_parts} - {part.name}",
                                            progress=upl_progress_cb, progress_args=(part.stat().st_size,))
                # notify admin/log
                await send_log(f"Uploaded part {idx}/{total_parts}: {part.name}")
            except Exception as e:
                log.exception("upload failed")
                try:
                    await edit_status(f"❌ Upload failed for {part.name}: {e}")
                    await send_log(f"Upload failed for {part.name}: {e}")
                except Exception:
                    pass
                return

        await edit_status(f"✅ All done. Uploaded {len(parts)} file(s).")
        await send_log(f"Completed leech: {url} -> {len(parts)} parts")
    finally:
        # cleanup
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass
        # remove session
        SESS.pop(token, None)

# ----------------- Run -----------------
if __name__ == "__main__":
    log.info("Starting leech-bot (PID %s)", os.getpid())
    app.run()
