#!/usr/bin/env python
# main.py
"""
Telegram Video Leech Bot
- Compatible with Windows GitHub Actions runner + choco ffmpeg
- Unique resolution buttons up to 1080p
- Download + upload progress updates (throttled to 5s)
- Remux -> re-encode fallback
- Split > ~1.95 GiB into streamable mp4 parts (time-based)
- Sends resulting file(s) to TG_CHAT (env)
"""

import os
import sys
import uuid
import time
import math
import shutil
import tempfile
import logging
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional

# External libs (installed in workflow)
import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

# --------- Config / Env ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("leech-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
TG_CHAT = os.getenv("TG_CHAT", "")  # either integer id (string) or @username
ALLOWED_USERS = [x.strip() for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()]

# Limits / behavior
PART_MAX_BYTES = int(float(os.getenv("PART_MAX_GB", "1.95")) * (1024 ** 3))  # default 1.95 GiB
PROGRESS_THROTTLE = int(os.getenv("PROGRESS_UPDATE_INTERVAL", "5"))  # seconds

# Sanity
if not BOT_TOKEN or not API_ID or not API_HASH or not TG_CHAT:
    log.critical("Please set BOT_TOKEN, API_ID, API_HASH and TG_CHAT environment variables.")
    sys.exit(1)

# --------- Pyrogram client ----------
app = Client("leech-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --------- Helpers: find ffmpeg/ffprobe ----------
def _which_any(names: List[str]) -> Optional[str]:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None

def find_ffmpeg() -> str:
    # check PATH first
    p = _which_any(["ffmpeg", "ffmpeg.exe"])
    if p:
        return p
    # common choco path
    choco_ff = Path("C:/ProgramData/chocolatey/lib/ffmpeg/tools/ffmpeg/bin/ffmpeg.exe")
    if choco_ff.exists():
        return str(choco_ff)
    raise RuntimeError("ffmpeg not found. Install ffmpeg or ensure it's on PATH.")

def find_ffprobe() -> str:
    p = _which_any(["ffprobe", "ffprobe.exe"])
    if p:
        return p
    choco_probe = Path("C:/ProgramData/chocolatey/lib/ffmpeg/tools/ffmpeg/bin/ffprobe.exe")
    if choco_probe.exists():
        return str(choco_probe)
    raise RuntimeError("ffprobe not found. Install ffmpeg or ensure it's on PATH.")

FFMPEG_BIN = find_ffmpeg()
FFPROBE_BIN = find_ffprobe()
log.info("Using ffmpeg: %s, ffprobe: %s", FFMPEG_BIN, FFPROBE_BIN)

# --------- Utilities ----------
def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return str(user_id) in ALLOWED_USERS or str(user_id) == str(TG_CHAT)

async def send_log(text: str):
    try:
        # let TG_CHAT be number or username
        chat = int(TG_CHAT) if str(TG_CHAT).lstrip("-").isdigit() else TG_CHAT
        await app.send_message(chat, text)
    except Exception:
        log.exception("Cannot send log message")

def unique_formats(formats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # pick single best format per numeric resolution (height), cap <=1080
    by_h: Dict[int, Dict[str, Any]] = {}
    for f in formats:
        if f.get("vcodec") == "none":  # skip audio only
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
    # sort descending height
    return [by_h[h] for h in sorted(by_h.keys(), reverse=True)]

# --------- Subprocess helpers (async) ----------
async def run_cmd(cmd: List[str], cwd: Optional[str] = None, timeout: Optional[int] = None):
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")

async def remux_streamable(src: Path, dst: Path):
    cmd = [FFMPEG_BIN, "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dst)]
    code, out, err = await run_cmd(cmd)
    if code != 0:
        raise RuntimeError(f"ffmpeg remux failed: {err}")

async def reencode_streamable(src: Path, dst: Path):
    cmd = [FFMPEG_BIN, "-y", "-i", str(src),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)]
    code, out, err = await run_cmd(cmd)
    if code != 0:
        raise RuntimeError(f"ffmpeg reencode failed: {err}")

async def get_duration(path: Path) -> float:
    cmd = [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    code, out, err = await run_cmd(cmd)
    if code != 0:
        log.warning("ffprobe failed: %s", err)
        return 0.0
    try:
        return float(out.strip())
    except:
        return 0.0

async def split_by_time(src: Path, out_dir: Path, max_bytes: int = PART_MAX_BYTES) -> List[Path]:
    size = src.stat().st_size
    if size <= max_bytes:
        return [src]
    duration = await get_duration(src)
    if duration <= 0:
        raise RuntimeError("Cannot determine duration for splitting")
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
        cmd = [FFMPEG_BIN, "-y", "-ss", str(start), "-i", str(src), "-t", str(seg_secs), "-c", "copy", "-movflags", "+faststart", str(out_file)]
        code, out, err = await run_cmd(cmd)
        if code != 0:
            raise RuntimeError(f"ffmpeg split failed at {start}s: {err}")
        parts.append(out_file)
    # ensure parts sizes ok
    for p in parts:
        if p.stat().st_size > max_bytes + 1024 * 1024:
            raise RuntimeError(f"Part too large after split: {p} size {p.stat().st_size}")
    return parts

# --------- Progress throttling ----------
class ThrottledNotifier:
    def __init__(self, min_interval: int = PROGRESS_THROTTLE):
        self.min_interval = min_interval
        self._last: Dict[str, float] = {}

    def should_update(self, key: str) -> bool:
        now = time.monotonic()
        last = self._last.get(key, 0)
        if now - last >= self.min_interval:
            self._last[key] = now
            return True
        return False

notifier = ThrottledNotifier(PROGRESS_THROTTLE)

# --------- Bot flow ----------
SESSIONS: Dict[str, Dict[str, Any]] = {}  # token -> {url, info, requested_by}

@app.on_message(filters.command("start") & filters.private)
async def cmd_start(_, m: Message):
    await m.reply("Hello — send a video URL (private chat). I'll show resolutions (up to 1080p).")

@app.on_message(filters.command("help") & filters.private)
async def cmd_help(_, m: Message):
    await m.reply("/start /help /leech <url> — or just send a URL in private chat.")

@app.on_message(filters.command("leech") & filters.private)
async def cmd_leech(_, m: Message):
    if not is_allowed(m.from_user.id):
        await m.reply("⛔ You are not allowed.")
        return
    if len(m.command) < 2:
        await m.reply("Usage: /leech <url>")
        return
    url = m.text.split(None, 1)[1].strip()
    await handle_url(m, url)

@app.on_message(filters.text & filters.private)
async def on_text(_, m: Message):
    if not is_allowed(m.from_user.id):
        await m.reply("⛔ You are not allowed.")
        return
    url = m.text.strip()
    await handle_url(m, url)

async def handle_url(message: Message, url: str):
    status = await message.reply_text("🔎 Fetching formats (please wait)...")
    try:
        def fetch():
            opts = {"quiet": True, "skip_download": True, "no_warnings": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, fetch)
        formats = info.get("formats", []) or []
        video_formats = [f for f in formats if f.get("vcodec") != "none"]
        unique = unique_formats(video_formats)
        if not unique:
            await status.edit_text("❌ No video formats found (or unsupported).")
            return
        token = uuid.uuid4().hex
        SESSIONS[token] = {"url": url, "info": info, "requested_by": message.from_user.id}
        # build keyboard: show label and format id in callback
        kb = []
        for f in unique:
            h = f.get("height") or 0
            label = f"{h}p" if h > 0 else (f.get("format_note") or "auto")
            fmt_id = f.get("format_id")
            kb.append([InlineKeyboardButton(label, callback_data=f"LEECH|{token}|{fmt_id}")])
        kb.append([InlineKeyboardButton("Cancel ❌", callback_data=f"CANCEL|{token}")])
        await status.edit_text("Select resolution:", reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        log.exception("format fetch fail")
        await status.edit_text(f"❌ Error fetching formats: {e}")
        await send_log(f"Error fetching formats for {url}: {e}")

@app.on_callback_query()
async def on_callback(_, cq: CallbackQuery):
    data = cq.data or ""
    if data.startswith("CANCEL|"):
        token = data.split("|", 1)[1]
        SESSIONS.pop(token, None)
        await cq.message.edit_text("Cancelled.")
        await cq.answer()
        return
    if not data.startswith("LEECH|"):
        await cq.answer()
        return
    _, token, fmt_id = data.split("|", 2)
    session = SESSIONS.get(token)
    if not session:
        await cq.answer("Session expired", show_alert=True)
        return
    url = session["url"]
    # reply with queued status and use .id (pyrogram v3 uses .id)
    status_msg = await cq.message.reply_text(f"Queued: {url}\nFormat: {fmt_id}")
    await cq.answer("Queued")
    # run pipeline in background
    asyncio.create_task(handle_leech_pipeline(cq, session, fmt_id, status_msg.id))

async def handle_leech_pipeline(cq_source: CallbackQuery, session: Dict[str, Any], format_id: str, status_msg_id: int):
    # cq_source is used only to access app (cq_source._client or we can use global app)
    url = session["url"]
    requested_by = session.get("requested_by")
    tmpdir = Path(tempfile.mkdtemp(prefix="leech_"))
    try:
        # send starting log
        await send_log(f"Starting leech: {url} (format {format_id})")
        # create helper to edit status message in chat where user pressed button
        chat_id = cq_source.message.chat.id
        async def edit_status(text: str):
            try:
                await app.edit_message_text(chat_id=chat_id, message_id=status_msg_id, text=text)
            except Exception:
                try:
                    await app.send_message(chat_id, text)
                except Exception:
                    pass

        # progress hook for yt-dlp (throttled)
        last_key = f"dl:{uuid.uuid4().hex}"
        def ytdl_hook(d):
            # d contains status 'downloading' or 'finished'
            try:
                status_name = d.get("status")
                if status_name == "downloading":
                    downloaded = d.get("downloaded_bytes") or 0
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    speed = d.get("speed") or 0
                    eta = d.get("eta") or 0
                    percent = (downloaded / total * 100) if total else 0.0
                    key = f"dl-{token if (token := session.get('url')) else last_key}"
                    if notifier.should_update(key):
                        text = f"⬇️ Downloading: {d.get('filename','')}\n{percent:.2f}% • {downloaded//1024} KB / {total//1024 if total else 0} KB\nSpeed: {int(speed)//1024 if speed else 0} KB/s • ETA: {int(eta)}s"
                        asyncio.create_task(edit_status(text))
                elif status_name == "finished":
                    if notifier.should_update("dl-finished"):
                        asyncio.create_task(edit_status("⬇️ Download finished. Finalizing..."))
            except Exception:
                log.exception("ytdl hook error")

        # Prepare yt-dlp options
        outtmpl = str(tmpdir / "%(title).200s.%(ext)s")
        ydl_opts = {
            "format": format_id,
            "outtmpl": outtmpl,
            "noplaylist": True,
            "progress_hooks": [ytdl_hook],
            "quiet": True,
            "no_warnings": True,
            # ensure ffmpeg usage from found path
            "ffmpeg_location": str(Path(FFMPEG_BIN).parent) if FFMPEG_BIN else None,
        }
        loop = asyncio.get_event_loop()
        def run_ydl():
            with yt_dlp.YoutubeDL({k: v for k, v in ydl_opts.items() if v is not None}) as ydl:
                return ydl.extract_info(url, download=True)
        try:
            info = await loop.run_in_executor(None, run_ydl)
        except Exception as e:
            log.exception("yt-dlp download failed")
            await edit_status(f"❌ yt-dlp failed: {e}")
            await send_log(f"yt-dlp failed for {url}: {e}")
            return

        # locate downloaded file (largest)
        files = sorted([p for p in tmpdir.iterdir() if p.is_file()], key=lambda p: p.stat().st_size, reverse=True)
        if not files:
            await edit_status("❌ Download produced no files.")
            await send_log(f"No output file for {url}")
            return
        downloaded_file = files[0]
        await edit_status(f"⬇️ Download complete: {downloaded_file.name}")

        # remux -> reencode fallback
        remuxed = tmpdir / f"{downloaded_file.stem}.streamable.mp4"
        src_for_split = downloaded_file
        try:
            await edit_status("🔧 Remuxing to streamable MP4...")
            await remux_streamable(downloaded_file, remuxed)
            src_for_split = remuxed
        except Exception:
            await edit_status("⚠️ Remux failed, trying re-encode (slower)...")
            try:
                reenc = tmpdir / f"{downloaded_file.stem}.reenc.mp4"
                await reencode_streamable(downloaded_file, reenc)
                src_for_split = reenc
            except Exception as e:
                log.exception("reencode failed")
                await edit_status(f"❌ Remux & re-encode failed: {e}")
                await send_log(f"Remux & reencode failed for {downloaded_file}: {e}")
                return

        # split if needed
        size = src_for_split.stat().st_size
        if size > PART_MAX_BYTES:
            await edit_status(f"✂️ Splitting file ({size} bytes) into <= {PART_MAX_BYTES} bytes parts...")
            try:
                parts = await split_by_time(src_for_split, tmpdir, max_bytes=PART_MAX_BYTES)
            except Exception as e:
                log.exception("splitting failed")
                await edit_status(f"❌ Splitting failed: {e}")
                await send_log(f"Splitting failed for {src_for_split}: {e}")
                return
        else:
            parts = [src_for_split]

        # Upload with progress (throttled)
        total_parts = len(parts)
        for idx, part in enumerate(parts, start=1):
            await edit_status(f"📤 Uploading part {idx}/{total_parts}: {part.name}")
            total_bytes = part.stat().st_size

            last_upload_key = f"upl:{part.name}"
            def progress_cb(current, total, *args):
                try:
                    if notifier.should_update(last_upload_key):
                        pct = (current / total * 100) if total else 0.0
                        txt = f"⬆️ Uploading part {idx}/{total_parts}: {part.name}\n{pct:.2f}% • {current//1024} KB / {total//1024} KB"
                        asyncio.create_task(edit_status(txt))
                except Exception:
                    log.exception("upload progress cb error")

            try:
                # allow TG_CHAT to be numeric or username
                chat = int(TG_CHAT) if str(TG_CHAT).lstrip("-").isdigit() else TG_CHAT
                await app.send_document(chat, str(part), caption=f"Part {idx}/{total_parts} - {part.name}",
                                        progress=progress_cb, progress_args=(total_bytes,))
                # log to admin chat if different / or send message
                await send_log(f"✔️ Uploaded part {idx}/{total_parts}: {part.name}")
            except Exception as e:
                log.exception("upload failed")
                # try to notify user
                try:
                    await send_log(f"❌ Upload failed for {part.name}: {e}")
                    await edit_status(f"❌ Upload failed: {e}")
                except Exception:
                    pass
                return

        await edit_status(f"✅ All done. Uploaded {len(parts)} file(s).")
        await send_log(f"Completed: {url} -> {len(parts)} part(s)")
    finally:
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass

# --------- Run ----------
if __name__ == "__main__":
    log.info("Starting leech-bot...")
    app.run()
