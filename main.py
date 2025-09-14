# main.py
"""
Telegram Video Leech Bot (Pyrogram + yt-dlp + ffmpeg)
Features:
- Presents unique resolution buttons to user
- Download selected resolution
- Download progress updates every ~9 seconds
- Remux to streamable mp4 (-movflags +faststart)
- If output > PART_MAX_BYTES (1.95 GB) split into streamable mp4 parts via ffmpeg by time
- Upload parts sequentially with upload-progress updates every ~9 seconds
- Logs and errors sent to TG_CHAT
"""

import os
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

# -----------------------
# Configuration (env)
# -----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
TG_CHAT = int(os.getenv("TG_CHAT", "0"))  # Chat ID for logs
ALLOWED_USERS = [s.strip() for s in os.getenv("ALLOWED_USERS", "").split(",") if s.strip()]
# maximum part size: 1.95 GiB
PART_MAX_BYTES = int(float(os.getenv("PART_MAX_GB", "1.95")) * (1024 ** 3))
# progress update interval seconds (8-10s requested) -> use 9s default
PROGRESS_UPDATE_INTERVAL = int(os.getenv("PROGRESS_UPDATE_INTERVAL", "9"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")
if not API_ID or not API_HASH:
    raise RuntimeError("API_ID / API_HASH not set")
if not TG_CHAT:
    raise RuntimeError("TG_CHAT not set (chat id where logs are sent)")

# -----------------------
# Pyrogram client
# -----------------------
app = Client("leech-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# -----------------------
# Utilities
# -----------------------
def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True  # if ALLOWED_USERS empty => allow all (but TG_CHAT still gets logs)
    return str(user_id) in ALLOWED_USERS or str(user_id) == str(TG_CHAT)

async def send_log(text: str):
    try:
        await app.send_message(TG_CHAT, text)
    except Exception as e:
        print("Failed sending log:", e)

def unique_formats_by_resolution(formats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    From yt-dlp formats list return one format per resolution (height).
    Prefer best quality per resolution (highest tbr or filesize).
    """
    by_height: Dict[Optional[int], Dict[str, Any]] = {}
    for f in formats:
        # skip audio-only or no height
        height = f.get("height")
        if height is None:
            # for formats with no height (dash audio/video combined?), use format note
            # we skip audio-only (vcodec == 'none')
            if f.get("vcodec") == "none":
                continue
            # treat unknown height as 0 (lowest priority)
            height = 0

        # choose best candidate by filesize or tbr if present
        cur = by_height.get(height)
        # define score
        score = (f.get("filesize") or 0) + int((f.get("tbr") or 0) * 1024)
        cur_score = 0
        if cur:
            cur_score = (cur.get("filesize") or 0) + int((cur.get("tbr") or 0) * 1024)
        if not cur or score > cur_score:
            by_height[height] = f
    # return sorted descending by height
    return [by_height[h] for h in sorted(by_height.keys(), reverse=True)]

# -----------------------
# yt-dlp progress hook with throttled updates
# -----------------------
class ProgressNotifier:
    def __init__(self, chat_id: int, message_id: Optional[int], update_fn, min_interval: int = PROGRESS_UPDATE_INTERVAL):
        self.chat_id = chat_id
        self.message_id = message_id
        self.update_fn = update_fn  # coroutine to call with text
        self.min_interval = min_interval
        self._last_update = 0

    async def maybe_update(self, text: str, force: bool = False):
        now = time.monotonic()
        if force or (now - self._last_update >= self.min_interval):
            await self.update_fn(text)
            self._last_update = now

# -----------------------
# Download & split helpers
# -----------------------
async def run_subprocess(cmd: List[str], cwd: Optional[str] = None):
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd)
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="ignore"), stderr.decode(errors="ignore")

async def remux_to_streamable_mp4(src: Path, dst: Path) -> None:
    """
    Run ffmpeg to create streamable MP4 with faststart without re-encoding if possible.
    Uses -c copy and -movflags +faststart
    """
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-c", "copy",
        "-movflags", "+faststart",
        str(dst)
    ]
    code, out, err = await run_subprocess(cmd)
    if code != 0:
        raise RuntimeError(f"ffmpeg remux failed: {err}")

async def get_duration_seconds(path: Path) -> float:
    # use ffprobe to get duration
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    code, out, err = await run_subprocess(cmd)
    if code != 0 or not out.strip():
        # fallback to yt-dlp info or 0
        return 0.0
    try:
        return float(out.strip())
    except:
        return 0.0

async def split_mp4_by_time(src: Path, out_dir: Path, max_bytes: int = PART_MAX_BYTES) -> List[Path]:
    """
    Split src into multiple MP4 parts where each part <= max_bytes.
    Strategy:
      - Determine duration (seconds) and filesize (bytes).
      - Compute bytes_per_second = filesize / duration
      - segment_seconds = floor(max_bytes / bytes_per_second)
      - Create segments using ffmpeg -ss start -t segment_seconds -c copy -movflags +faststart
    Returns list of part file paths.
    """
    size = src.stat().st_size
    if size <= max_bytes:
        return [src]

    duration = await get_duration_seconds(src)
    if not duration or duration <= 0:
        # fallback: attempt to split roughly by size using byte-reading (less ideal)
        # We'll perform raw binary splitting -> but raw binary parts won't be independently streamable.
        # So instead throw error (we need duration to split properly).
        raise RuntimeError("Cannot determine duration for splitting; aborting splitting to preserve streamability.")

    bytes_per_sec = size / duration
    # ensure at least 5 seconds per segment
    seg_secs = max(5, int(math.floor(max_bytes / bytes_per_sec)))
    if seg_secs <= 0:
        seg_secs = 10

    parts: List[Path] = []
    total_secs = int(math.ceil(duration))
    part_index = 0
    for start in range(0, total_secs, seg_secs):
        part_index += 1
        out_file = out_dir / f"{src.stem}.part{part_index:02d}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", str(src),
            "-t", str(seg_secs),
            "-c", "copy",
            "-movflags", "+faststart",
            str(out_file)
        ]
        code, out, err = await run_subprocess(cmd)
        if code != 0:
            raise RuntimeError(f"ffmpeg split failed at start {start}: {err}")
        parts.append(out_file)
    # final safety: ensure each part <= max_bytes
    for p in parts:
        if p.stat().st_size > max_bytes + 1024 * 1024:  # allow small slack 1MB
            raise RuntimeError(f"Part too large after split: {p} ({p.stat().st_size} bytes)")
    return parts

# -----------------------
# Main interactive flow
# -----------------------
# store ephemeral context for each user -> mapping msg_id or chat to info
SESSIONS: Dict[int, Dict[str, Any]] = {}

@app.on_message(filters.command("start") & filters.private)
async def start_msg(c: Client, m: Message):
    if not is_allowed(m.from_user.id):
        await m.reply("⛔ You are not allowed to use this bot.")
        return
    await m.reply("Hello! Send me a video URL. I'll show available resolutions to pick from.")

@app.on_message(filters.text & filters.private)
async def on_text(c: Client, m: Message):
    # Accept plain URL and present resolution buttons
    if not is_allowed(m.from_user.id):
        await m.reply("⛔ You are not allowed to use this bot.")
        return

    url = m.text.strip()
    status = await m.reply_text("🔎 Fetching formats, please wait...")
    try:
        # get formats info (no download)
        ydl_opts = {"quiet": True, "skip_download": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        formats = info.get("formats", [])
        # filter only video (has vcodec not 'none') and container useful
        video_formats = [f for f in formats if f.get("vcodec") != "none"]
        unique = unique_formats_by_resolution(video_formats)
        if not unique:
            await status.edit_text("No video formats found.")
            return

        # build keyboard: resolution label -> format id
        keyboard = []
        for f in unique:
            h = f.get("height") or 0
            # label like "1080p" or "audio+video"
            label = f"{h}p" if h > 0 else (f.get("format_note") or "auto")
            fmt_id = f.get("format_id")
            # attach as data: "LEECH|url|format_id"
            data = f"LEECH|{url}|{fmt_id}"
            keyboard.append([InlineKeyboardButton(label, callback_data=data)])
        # add a cancel button
        keyboard.append([InlineKeyboardButton("Cancel ❌", callback_data=f"CANCEL|{url}")])

        await status.edit_text("Select resolution to download:", reply_markup=InlineKeyboardMarkup(keyboard))
        # store context
        SESSIONS[m.from_user.id] = {"info": info, "message_id": status.message_id}
    except Exception as e:
        await status.edit_text(f"Error fetching formats: {e}")
        await send_log(f"Error fetching formats for {url}: {e}")

@app.on_callback_query()
async def on_callback(c: Client, cq: CallbackQuery):
    data = cq.data
    user_id = cq.from_user.id
    if not is_allowed(user_id):
        await cq.answer("Access denied.", show_alert=True)
        return

    if not data:
        await cq.answer()
        return

    if data.startswith("CANCEL|"):
        await cq.message.edit_text("Cancelled.")
        await cq.answer("Cancelled")
        return

    # format: LEECH|<url>|<format_id>
    parts = data.split("|", 2)
    if len(parts) != 3:
        await cq.answer()
        return
    _, url, format_id = parts

    await cq.answer("Queued for download...")
    msg = await cq.message.reply_text(f"Queued: {url}\nFormat: {format_id}")

    # Launch asynchronous task
    asyncio.create_task(handle_leech(c, cq.from_user.id, cq.message.chat.id, url, format_id, msg.message_id))

async def handle_leech(client: Client, user_id: int, chat_id: int, url: str, format_id: str, status_message_id: int):
    """Main pipeline: download -> remux -> split if needed -> upload parts"""
    # Prepare temp dir
    tmpdir = Path(tempfile.mkdtemp(prefix="leech_"))
    try:
        await client.send_message(chat_id, f"⏬ Starting download for selected format `{format_id}`\nURL: {url}")
        # create status updater
        async def edit_status(text):
            try:
                await client.edit_message_text(chat_id=chat_id, message_id=status_message_id, text=text)
            except Exception:
                # fallback to sending new message
                await client.send_message(chat_id, text)

        notifier = ProgressNotifier(chat_id, status_message_id, edit_status, PROGRESS_UPDATE_INTERVAL)

        # yt-dlp download with progress hook
        ydl_opts = {
            "format": format_id,
            "outtmpl": str(tmpdir / "%(title).200s.%(ext)s"),
            "noplaylist": True,
            "progress_hooks": []
        }

        last_hook_time = 0

        def ytdl_hook(d):
            nonlocal last_hook_time
            now = time.monotonic()
            status_text = ""
            if d["status"] == "downloading":
                downloaded = d.get("downloaded_bytes") or d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                speed = d.get("speed") or 0
                eta = d.get("eta") or 0
                percent = (downloaded / total * 100) if total else 0.0
                status_text = f"⬇️ Downloading: {d.get('filename', '')}\n{percent:.2f}% • {downloaded//1024} KB / {total//1024 if total else 0} KB\nSpeed: {int(speed)//1024 if speed else 0} KB/s • ETA: {int(eta)}s"
            elif d["status"] == "finished":
                status_text = "⬇️ Download finished. Finalizing..."
            # throttle to PROGRESS_UPDATE_INTERVAL from last_hook_time
            if time.monotonic() - last_hook_time >= PROGRESS_UPDATE_INTERVAL:
                # schedule async update
                asyncio.get_event_loop().create_task(notifier.maybe_update(status_text, force=True))
                last_hook_time = time.monotonic()

        ydl_opts["progress_hooks"].append(ytdl_hook)

        # run yt-dlp
        loop = asyncio.get_event_loop()
        def run_ydl():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return info
        info = await loop.run_in_executor(None, run_ydl)
        # find downloaded file (best guess)
        entries = info.get("_filename") or info.get("requested_downloads", [])
        # yt-dlp sets "requested_downloads" sometimes; but easiest to scan tmpdir
        files = list(tmpdir.iterdir())
        if not files:
            await client.send_message(chat_id, "❌ Download produced no files.")
            await send_log(f"Download produced no files for {url}")
            return
        # pick largest file
        downloaded_file = max(files, key=lambda p: p.stat().st_size)
        await notifier.maybe_update(f"⬇️ Download complete: {downloaded_file.name}", force=True)

        # Remux to streamable mp4 if not mp4 or ensure faststart
        remuxed = tmpdir / f"{downloaded_file.stem}.streamable.mp4"
        try:
            await notifier.maybe_update("🔧 Remuxing to streamable MP4...")
            await remux_to_streamable_mp4(downloaded_file, remuxed)
            src_for_split = remuxed
        except Exception as e:
            # if remux failed, fallback to original file (may still be mp4)
            await notifier.maybe_update(f"⚠️ Remux failed, using original file: {e}")
            src_for_split = downloaded_file

        # Check size and split if needed
        size = src_for_split.stat().st_size
        if size > PART_MAX_BYTES:
            await notifier.maybe_update(f"✂️ File {src_for_split.name} is {size} bytes; splitting into <= {PART_MAX_BYTES} bytes parts...")
            parts = await split_mp4_by_time(src_for_split, tmpdir, max_bytes=PART_MAX_BYTES)
        else:
            parts = [src_for_split]

        # Upload parts sequentially with progress
        async def upload_file(part_path: Path):
            total = part_path.stat().st_size
            last_upload_update = 0

            async def progress_callback(current, total_bytes):
                nonlocal last_upload_update
                now = time.monotonic()
                if now - last_upload_update >= PROGRESS_UPDATE_INTERVAL:
                    percent = current / total_bytes * 100 if total_bytes else 0
                    text = f"⬆️ Uploading: {part_path.name}\n{percent:.2f}% • {current//1024} KB / {total_bytes//1024} KB"
                    try:
                        await client.send_message(chat_id, text)
                    except Exception:
                        pass
                    last_upload_update = now

            # Pyrogram's send_document / send_video supports progress callback via "progress" param in sync .upload methods? We use send_document with file path and a progress handler by using progress parameter in send_document
            # For async client, progress parameter is named "progress" and "progress_args".
            await client.send_document(chat_id, str(part_path), caption=f"Part: {part_path.name}", progress=progress_callback, progress_args=(part_path.stat().st_size,))

        # iterate uploads
        for idx, p in enumerate(parts, start=1):
            await notifier.maybe_update(f"📤 Uploading part {idx}/{len(parts)}: {p.name}", force=True)
            try:
                await upload_file(p)
                await client.send_message(TG_CHAT, f"✔️ Uploaded part {idx}/{len(parts)}: {p.name}")
            except Exception as e:
                await client.send_message(TG_CHAT, f"❌ Upload failed for {p.name}: {e}")
                # continue with next or abort? We'll abort to avoid partial state
                await client.send_message(chat_id, f"❌ Upload failed: {e}")
                return

        await client.send_message(chat_id, f"✅ All done. Uploaded {len(parts)} file(s).")
        await send_log(f"Completed leech for {url}. Parts: {len(parts)}")
    except Exception as e:
        await client.send_message(chat_id, f"❌ Error in leech pipeline: {e}")
        await send_log(f"Error while leeching {url}: {e}")
    finally:
        # cleanup temp files
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass

# -----------------------
# Run
# -----------------------
if __name__ == "__main__":
    print("Starting leech-bot...")
    app.run()
