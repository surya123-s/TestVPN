#!/usr/bin/env python3
# main.py
"""
Telegram Video Leech Bot - with Progress Bars and Cloudflare / extractor retries

Preserves all previous messages/features:
- resolution buttons (unique up to 1080p)
- download progress bar (5s throttle) with MB/s
- upload progress bar (5s) with MB/s
- remux to streamable mp4 / fallback re-encode
- split > PART_MAX_GB (default 1.95 GiB)
- send files into TG_CHAT
- ALLOWED_USERS enforcement
- robust retries for Cloudflare / xHamster extractor errors
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
from yt_dlp.utils import DownloadError, ExtractorError
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

# ----------------- Configuration -----------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("leech-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", "0") or 0)
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
    pct = max(0.0, min(100.0, pct))
    filled = int(round(length * pct / 100))
    empty = length - filled
    return "▰" * filled + "▱" * empty

# ----------------- Utilities -----------------
def is_allowed(uid: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return str(uid) in ALLOWED_USERS or str(uid) == str(TG_CHAT)

async def send_log(text: str):
    """Send log to TG_CHAT (best-effort)."""
    try:
        target = int(TG_CHAT) if str(TG_CHAT).lstrip("-").isdigit() else TG_CHAT
        await app.send_message(target, text)
    except Exception:
        log.exception("Failed to send admin log")

def human_mb(b: int) -> str:
    return f"{b / (1024*1024):.2f} MB"

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

# ----------------- Extractor helpers (retries) -----------------
def try_extract_info(url: str, ydl_base_opts: Dict[str, Any]) -> Dict[str, Any]:
    """
    Try several extractor configurations to handle Cloudflare / site changes.
    Order:
      1) default
      2) impersonation (generic:impersonate=chrome110)
      3) add explicit HTTP headers (User-Agent)
      4) add generic:impersonate + headers (explicit)
    Returns info dict or raises last exception.
    """
    attempts = []
    # base opts copy
    base = dict(ydl_base_opts)

    # Candidate 1: base
    attempts.append(base)

    # Candidate 2: impersonation via extractor_args generic:impersonate (profile)
    impersonate_opts = dict(base)
    impersonate_opts.setdefault("extractor_args", {})
    # use profile name; recent yt-dlp supports "chrome110", "firefox119" etc.
    # If profile is not supported by installed yt-dlp impersonation engine, extractor may still fail.
    impersonate_opts["extractor_args"]["generic"] = {"impersonate": "chrome110"}
    attempts.append(impersonate_opts)

    # Candidate 3: set explicit HTTP headers (common browser UA)
    headers_opts = dict(base)
    headers_opts["http_headers"] = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/117.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }
    attempts.append(headers_opts)

    # Candidate 4: impersonation + headers
    imp_headers = dict(headers_opts)
    imp_headers.setdefault("extractor_args", {})
    imp_headers["extractor_args"]["generic"] = {"impersonate": "chrome110"}
    attempts.append(imp_headers)

    last_exc = None
    for idx, opts in enumerate(attempts, start=1):
        try:
            log.info("Extractor attempt %d for %s", idx, url)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                # if site returned partial info but formats empty, treat as failure
                if not info:
                    raise ExtractorError("Empty info returned")
                return info
        except Exception as e:
            last_exc = e
            log.warning("Extractor attempt %d failed: %s", idx, e)
            # keep trying next
            continue
    # all failed
    raise last_exc

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
        # base ydl options for info fetch
        ydl_base_opts = {"quiet": True, "skip_download": True, "no_warnings": True}
        loop = asyncio.get_event_loop()
        # run the robust extractor in executor
        try:
            info = await loop.run_in_executor(None, try_extract_info, url, ydl_base_opts)
        except Exception as e:
            # send helpful message to user and admins
            log.exception("fetch formats failed")
            await status.edit_text(f"❌ Error fetching formats: {e}")
            await send_log(f"Error fetching formats for {url}: {e}")
            return

        formats = info.get("formats", []) or []
        video_formats = [f for f in formats if f.get("vcodec") != "none"]
        if not video_formats:
            await status.edit_text("❌ No video formats found.")
            return
        # deduplicate by height and prefer best per resolution
        by_h: Dict[int, Dict[str, Any]] = {}
        for f in video_formats:
            h = f.get("height") or 0
            if h > 1080:
                continue
            cur = by_h.get(h)
            score = (f.get("filesize") or 0) + int((f.get("tbr") or 0) * 1024)
            if not cur or score > ((cur.get("filesize") or 0) + int((cur.get("tbr") or 0) * 1024)):
                by_h[h] = f
        unique = [by_h[h] for h in sorted(by_h.keys(), reverse=True)]
        token = uuid.uuid4().hex
        SESS[token] = {"url": url, "info": info, "requested_by": message.from_user.id}
        kb = []
        for f in unique:
            h = f.get("height") or 0
            fmtid = f.get("format_id")
            label = f"{h}p" if h else "auto"
            kb.append([InlineKeyboardButton(label, callback_data=f"LEECH|{token}|{fmtid}")])
        kb.append([InlineKeyboardButton("Cancel ❌", callback_data=f"CANCEL|{token}")])
        await status.edit_text("Select resolution:", reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        log.exception("Unexpected failure in handle_incoming_url")
        await status.edit_text(f"❌ Error: {e}")
        await send_log(f"Unhandled error while fetching formats for {url}: {e}")

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
    # reply queued and capture status message id (pyrogram Message has .id)
    status_msg = await cq.message.reply_text(f"Queued: {url}\nFormat: {fmtid}")
    await cq.answer("Queued")
    # start pipeline in background
    asyncio.create_task(pipeline_task(token, sess, fmtid, status_msg.chat.id, status_msg.id))

# ----------------- Pipeline -----------------
async def pipeline_task(token: str, session: Dict[str, Any], fmtid: str, status_chat_id: int, status_msg_id: int):
    url = session["url"]
    tmpdir = Path(tempfile.mkdtemp(prefix="leech_"))
    try:
        async def edit_status(text: str):
            try:
                # use named parameters to be explicit
                await app.edit_message_text(chat_id=status_chat_id, message_id=status_msg_id, text=text)
            except Exception:
                try:
                    await app.send_message(status_chat_id, text)
                except Exception:
                    log.exception("Both edit_message_text and send_message failed for status updates")

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
                        bar = make_progress_bar(percent, 10)
                        txt = (
                            f"⬇️ Downloading: {d.get('filename','')}\n"
                            f"Progress: {bar} {percent:.2f}%\n"
                            f"{human_mb(int(downloaded))} / {human_mb(int(total))}\n"
                            f"Speed: {speed / (1024*1024):.2f} MB/s • ETA: {int(eta)}s"
                        )
                        coro = edit_status(txt)
                        # schedule safely from thread
                        loop.call_soon_threadsafe(asyncio.create_task, coro)
                elif st == "finished":
                    coro = edit_status("⬇️ Download finished. Processing...")
                    loop.call_soon_threadsafe(asyncio.create_task, coro)
            except Exception:
                log.exception("ytdl hook error")

        # build yt-dlp options for download (include ffmpeg if available)
        ydl_opts = {
            "format": fmtid,
            "outtmpl": str(tmpdir / "%(title).200s.%(ext)s"),
            "noplaylist": True,
            "progress_hooks": [ytdl_hook],
            "quiet": True,
            "no_warnings": True,
        }

        def run_ydl():
            # Use the robust extractor attempts when retrieving formats earlier, but here
            # it's faster to run single yt-dlp with given opts (progress hook).
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)

        try:
            # run download in a thread to avoid blocking loop
            info = await asyncio.get_event_loop().run_in_executor(None, run_ydl)
        except Exception as e:
            log.exception("yt-dlp failed during download")
            await edit_status(f"❌ yt-dlp failed: {e}")
            await send_log(f"yt-dlp download error for {url}: {e}")
            return

        # find downloaded file(s)
        files = sorted([p for p in tmpdir.iterdir() if p.is_file()], key=lambda p: p.stat().st_size, reverse=True)
        if not files:
            await edit_status("❌ No output file.")
            await send_log(f"No output file after yt-dlp for {url}")
            return
        downloaded = files[0]
        await edit_status(f"⬇️ Download complete: {downloaded.name}")

        # Remux to streamable mp4 (faststart), else re-encode
        remuxed = tmpdir / f"{downloaded.stem}.streamable.mp4"
        src_for_split = downloaded
        try:
            await edit_status("🔧 Remuxing to streamable MP4...")
            # use ffmpeg subprocess
            proc = asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(downloaded), "-c", "copy", "-movflags", "+faststart", str(remuxed),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            code, out = loop.run_until_complete(proc.communicate()) if False else None  # placeholder no-block
            # Instead run blocking in executor to be safe:
            def remux():
                import subprocess as sp
                sp.check_call([
                    "ffmpeg", "-y", "-i", str(downloaded), "-c", "copy", "-movflags", "+faststart", str(remuxed)
                ], stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            await loop.run_in_executor(None, remux)
            src_for_split = remuxed
        except Exception:
            # fallback re-encode (H.264 + AAC)
            await edit_status("⚠️ Remux failed, re-encoding (slower)...")
            try:
                reenc = tmpdir / f"{downloaded.stem}.reenc.mp4"
                def reencode():
                    import subprocess as sp
                    sp.check_call([
                        "ffmpeg", "-y", "-i", str(downloaded),
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                        "-c:a", "aac", "-b:a", "128k",
                        "-movflags", "+faststart",
                        str(reenc)
                    ], stdout=sp.DEVNULL, stderr=sp.DEVNULL)
                await loop.run_in_executor(None, reencode)
                src_for_split = reenc
            except Exception as e:
                log.exception("re-encode failed")
                await edit_status(f"❌ Remux & re-encode both failed: {e}")
                await send_log(f"Remux/reencode failed for {downloaded}: {e}")
                return

        # Split if bigger than allowed
        size = src_for_split.stat().st_size
        if size > PART_MAX_BYTES:
            await edit_status(f"✂️ Splitting into parts (<= {PART_MAX_GB} GiB)...")
            try:
                parts = await split_mp4_by_time(src_for_split, tmpdir, max_bytes=PART_MAX_BYTES)
            except Exception as e:
                log.exception("split failed")
                await edit_status(f"❌ Splitting failed: {e}")
                await send_log(f"Splitting failed for {src_for_split}: {e}")
                return
        else:
            parts = [src_for_split]

        # Upload parts sequentially
        total_parts = len(parts)
        for idx, part in enumerate(parts, start=1):
            await edit_status(f"📤 Uploading part {idx}/{total_parts}: {part.name}")
            last_key = f"upl:{token}:{part.name}"
            start_time = time.monotonic()

            # Accept flexible args: Pyrogram sometimes passes (current, total) or (current, total, some)
            def upl_progress_cb(*cb_args):
                try:
                    if len(cb_args) >= 2:
                        current = cb_args[0]
                        total = cb_args[1]
                    else:
                        return
                    if not total:
                        return
                    pct = (current / total) * 100
                    if throttler.should(last_key):
                        bar = make_progress_bar(pct, 10)
                        elapsed = max(1e-6, time.monotonic() - start_time)
                        speed = current / elapsed / (1024*1024)  # MB/s
                        txt = (
                            f"⬆️ Uploading: {part.name}\n"
                            f"Progress: {bar} {pct:.2f}%\n"
                            f"{human_mb(int(current))} / {human_mb(int(total))}\n"
                            f"Speed: {speed:.2f} MB/s"
                        )
                        coro = edit_status(txt)
                        loop.call_soon_threadsafe(asyncio.create_task, coro)
                except Exception:
                    log.exception("upload progress cb error")

            try:
                chat = int(TG_CHAT) if str(TG_CHAT).lstrip("-").isdigit() else TG_CHAT
                # prefer send_video (streamable mp4)
                if part.suffix.lower() == ".mp4":
                    await app.send_video(chat, str(part), caption=f"Part {idx}/{total_parts} - {part.name}",
                                         progress=upl_progress_cb, progress_args=(part.stat().st_size,))
                else:
                    await app.send_document(chat, str(part), caption=f"Part {idx}/{total_parts} - {part.name}",
                                            progress=upl_progress_cb, progress_args=(part.stat().st_size,))
                await send_log(f"✔️ Uploaded part {idx}/{total_parts}: {part.name}")
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
    except Exception as exc:
        log.exception("Unhandled error in pipeline")
        try:
            await app.send_message(status_chat_id, f"❌ Error in pipeline: {exc}")
        except Exception:
            pass
        await send_log(f"Unhandled pipeline error for {url}: {exc}")
    finally:
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass
        SESS.pop(token, None)

# ----------------- FFmpeg split helper -----------------
async def split_mp4_by_time(src: Path, dest_dir: Path, max_bytes: int = PART_MAX_BYTES) -> List[Path]:
    """
    Split src into streamable MP4 parts by time using ffmpeg copy.
    """
    # get filesize & duration using ffprobe
    import subprocess as sp
    size = src.stat().st_size
    if size <= max_bytes:
        return [src]
    # get duration (ffprobe)
    def get_duration():
        p = sp.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
                   capture_output=True, text=True)
        if p.returncode != 0:
            return 0.0
        try:
            return float(p.stdout.strip().splitlines()[0])
        except Exception:
            return 0.0
    duration = await asyncio.get_event_loop().run_in_executor(None, get_duration)
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
        out_file = dest_dir / f"{src.stem}.part{idx:02d}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", str(src),
            "-t", str(seg_secs),
            "-c", "copy",
            "-movflags", "+faststart",
            str(out_file)
        ]
        # run blocking ffmpeg in executor
        def run_cmd():
            import subprocess as sp2
            return sp2.run(cmd, stdout=sp2.PIPE, stderr=sp2.PIPE)
        res = await asyncio.get_event_loop().run_in_executor(None, run_cmd)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg split failed at start {start}: {res.stderr.decode()}")
        parts.append(out_file)
    # safety: ensure each part <= max_bytes + slack
    for p in parts:
        if p.stat().st_size > max_bytes + 1024 * 1024:
            raise RuntimeError(f"Part too large after split: {p} ({p.stat().st_size} bytes)")
    return parts

# ----------------- Run -----------------
if __name__ == "__main__":
    log.info("Starting bot")
    app.run()
