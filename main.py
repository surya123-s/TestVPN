#!/usr/bin/env python
# main.py (Stable Version)
"""
Telegram Video Leech Bot (Pyrogram + yt-dlp + ffmpeg)
- Unique resolutions only
- /start, /help, /leech <url>
- TG_CHAT logs
- File split at 1.95GB max
- Streamable MP4 output (remux → re-encode fallback)
- Progress updates every 9s
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

# requirement modules
import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

# ---------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("leech-bot")

# ---------- Python version guard ----------
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ required. Current version: %s" % sys.version.split()[0])

# ---------- yt-dlp auto update ----------
async def ensure_ytdlp_latest():
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "yt_dlp", "-U",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
    except Exception as e:
        log.warning("yt-dlp auto-update failed: %s", e)

# ---------- env config ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
TG_CHAT = int(os.getenv("TG_CHAT", "0"))
ALLOWED_USERS = [s.strip() for s in os.getenv("ALLOWED_USERS", "").split(",") if s.strip()]

PART_MAX_BYTES = int(float(os.getenv("PART_MAX_GB", "1.95")) * (1024 ** 3))
PROGRESS_UPDATE_INTERVAL = int(os.getenv("PROGRESS_UPDATE_INTERVAL", "9"))

if not BOT_TOKEN or not API_ID or not API_HASH or not TG_CHAT:
    raise RuntimeError("Missing BOT_TOKEN/API_ID/API_HASH/TG_CHAT in env")

# ---------- pyrogram client ----------
app = Client("leech-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---------- helpers ----------
def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or str(user_id) in ALLOWED_USERS or str(user_id) == str(TG_CHAT)

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
        h = f.get("height") or 0
        cur = by_height.get(h)
        score = (f.get("filesize") or 0) + int((f.get("tbr") or 0) * 1024)
        cur_score = (cur.get("filesize") or 0) + int((cur.get("tbr") or 0) * 1024) if cur else 0
        if not cur or score > cur_score:
            by_height[h] = f
    return [by_height[h] for h in sorted(by_height.keys(), reverse=True)]

class ProgressNotifier:
    def __init__(self, edit_cb, interval=PROGRESS_UPDATE_INTERVAL):
        self.cb = edit_cb
        self.interval = interval
        self._last = 0

    async def maybe_update(self, text: str, force=False):
        now = time.monotonic()
        if force or now - self._last >= self.interval:
            try:
                await self.cb(text)
            except Exception:
                pass
            self._last = now

async def run_cmd(cmd: List[str]) -> (int, str, str):
    proc = await asyncio.create_subprocess_exec(*cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")

async def remux(src: Path, dst: Path):
    code, _, err = await run_cmd(["ffmpeg","-y","-i",str(src),"-c","copy","-movflags","+faststart",str(dst)])
    if code != 0: raise RuntimeError(err)

async def reencode(src: Path, dst: Path):
    code, _, err = await run_cmd(["ffmpeg","-y","-i",str(src),"-c:v","libx264","-preset","veryfast",
                                  "-crf","23","-c:a","aac","-b:a","128k","-movflags","+faststart",str(dst)])
    if code != 0: raise RuntimeError(err)

async def duration(path: Path) -> float:
    code,out,_ = await run_cmd(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=duration",
                                "-of","default=noprint_wrappers=1:nokey=1",str(path)])
    try: return float(out.strip())
    except: return 0.0

async def split_mp4(src: Path, out_dir: Path, max_bytes=PART_MAX_BYTES) -> List[Path]:
    size = src.stat().st_size
    if size <= max_bytes: return [src]
    dur = await duration(src)
    if dur <= 0: raise RuntimeError("Duration unknown")
    seg = max(10, int(max_bytes / (size / dur)))
    parts=[]
    for i,start in enumerate(range(0, int(dur), seg),1):
        out=out_dir/f"{src.stem}.part{i:02d}.mp4"
        code,_,err=await run_cmd(["ffmpeg","-y","-ss",str(start),"-i",str(src),"-t",str(seg),"-c","copy","-movflags","+faststart",str(out)])
        if code!=0: raise RuntimeError(err)
        parts.append(out)
    return parts

SESSIONS: Dict[str, Dict[str, Any]] = {}

# ---------- commands ----------
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(_, m: Message):
    if not is_allowed(m.from_user.id): return await m.reply("⛔ Access Denied")
    await m.reply("✅ Bot running.\nSend URL or /leech <url>")

@app.on_message(filters.command("help") & filters.private)
async def help_cmd(_, m: Message):
    await m.reply("/leech <url> - leech video\nSend URL directly\nOnly ALLOWED_USERS may use")

@app.on_message(filters.command("leech") & filters.private)
async def leech_cmd(c, m: Message):
    if not is_allowed(m.from_user.id): return await m.reply("⛔")
    if len(m.command)<2: return await m.reply("Usage: /leech <url>")
    await handle_url(c,m,m.text.split(None,1)[1])

@app.on_message(filters.text & filters.private)
async def text_url(c,m): 
    if is_allowed(m.from_user.id): await handle_url(c,m,m.text.strip())

async def handle_url(c,m,url):
    status=await m.reply("🔎 Fetching formats...")
    try:
        def fetch():
            with yt_dlp.YoutubeDL({"quiet":True,"skip_download":True,"no_warnings":True}) as y: return y.extract_info(url,download=False)
        info=await asyncio.get_event_loop().run_in_executor(None,fetch)
        fmts=unique_formats_by_resolution([f for f in info.get("formats",[]) if f.get("vcodec")!="none"])
        if not fmts: return await status.edit("No video formats")
        token=uuid.uuid4().hex
        SESSIONS[token]={"url":url}
        kb=[[InlineKeyboardButton(f"{f.get('height') or ''}p",callback_data=f"LEECH:{token}:{f['format_id']}")] for f in fmts]
        kb.append([InlineKeyboardButton("Cancel ❌",callback_data=f"CANCEL:{token}")])
        await status.edit("Select resolution:",reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        await status.edit(f"❌ {e}")

@app.on_callback_query()
async def cb(c: Client, cq: CallbackQuery):
    if not is_allowed(cq.from_user.id): return await cq.answer("Denied",show_alert=True)
    d=cq.data
    if d.startswith("CANCEL:"):
        return await cq.message.edit("Cancelled")
    if d.startswith("LEECH:"):
        _,tok,fid=d.split(":",2)
        s=SESSIONS.get(tok); url=s["url"]
        msg=await cq.message.reply(f"Queued {fid}")
        asyncio.create_task(pipeline(c,cq.message.chat.id,url,fid,msg.message_id))

async def pipeline(c,chat_id,url,fid,msgid):
    tmp=Path(tempfile.mkdtemp())
    async def edit(t): 
        try: await c.edit_message_text(chat_id,msgid,t)
        except: pass
    notif=ProgressNotifier(edit)
    try:
        def dl():
            with yt_dlp.YoutubeDL({"format":fid,"outtmpl":str(tmp/"%(title).200s.%(ext)s"),
                                   "noplaylist":True,"quiet":True,"no_warnings":True}) as y: return y.extract_info(url,download=True)
        await asyncio.get_event_loop().run_in_executor(None,dl)
        files=sorted(tmp.glob("*"),key=lambda p:p.stat().st_size,reverse=True)
        f=files[0]
        mp4=tmp/f"{f.stem}.mp4"
        try: await remux(f,mp4)
        except: await reencode(f,mp4)
        parts=await split_mp4(mp4,tmp)
        for i,p in enumerate(parts,1):
            await notif.maybe_update(f"⬆️ Upload {i}/{len(parts)}",force=True)
            await c.send_document(chat_id,str(p),caption=f"Part {i}/{len(parts)} - {p.name}")
        await notif.maybe_update("✅ Done",force=True)
    except Exception as e:
        await edit(f"❌ {e}")
    finally:
        shutil.rmtree(tmp,ignore_errors=True)

# ---------- run ----------
if __name__=="__main__":
    asyncio.get_event_loop().run_until_complete(ensure_ytdlp_latest())
    app.run()
