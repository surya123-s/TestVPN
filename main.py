import os
import re
import asyncio
import shutil
import logging
import tempfile
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp

# -----------------------
# Config
# -----------------------
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
TG_CHAT = os.getenv("TG_CHAT")  # Chat username or numeric ID

PART_MAX_BYTES = int(1.95 * 1024 * 1024 * 1024)  # 1.95GB

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("leech-bot")

app = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# -----------------------
# Helpers
# -----------------------
def human_readable(size):
    power = 2 ** 10
    n = 0
    power_labels = {0: "B", 1: "KB", 2: "MB", 3: "GB", 4: "TB"}
    while size > power:
        size /= power
        n += 1
    return f"{size:.2f}{power_labels[n]}"

async def split_file(filepath: Path):
    parts = []
    total_size = filepath.stat().st_size
    if total_size <= PART_MAX_BYTES:
        return [filepath]
    with open(filepath, "rb") as f:
        idx = 1
        while True:
            chunk = f.read(PART_MAX_BYTES)
            if not chunk:
                break
            part_path = filepath.parent / f"{filepath.stem}.part{idx}{filepath.suffix}"
            with open(part_path, "wb") as p:
                p.write(chunk)
            parts.append(part_path)
            idx += 1
    return parts

# -----------------------
# Command Handlers
# -----------------------
@app.on_message(filters.command("start"))
async def start(client, message):
    await message.reply("👋 Send me a video URL to leech.")

@app.on_message(filters.text & ~filters.command("start"))
async def leech_handler(client, message):
    url = message.text.strip()
    status_msg = await message.reply("⏳ Queued...")

    # Build formats keyboard
    ydl_opts = {"listformats": True, "quiet": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get("formats", [])
    except Exception as e:
        return await status_msg.edit(f"❌ Error fetching formats: {e}")

    buttons = []
    seen_res = set()
    for f in formats:
        if not f.get("height"):
            continue
        res = f["height"]
        if res in seen_res:
            continue
        seen_res.add(res)
        fmt_id = f["format_id"]
        label = f"{res}p ({human_readable(f.get('filesize', 0) or 0)})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"dlinfo|{url}|{fmt_id}")])

    if not buttons:
        return await status_msg.edit("❌ No valid formats found.")

    await status_msg.edit("🎬 Choose quality:", reply_markup=InlineKeyboardMarkup(buttons))

@app.on_callback_query(filters.regex(r"^dlinfo\|"))
async def on_callback(client, cq):
    _, url, fmt_id = cq.data.split("|", 2)
    status_msg = await cq.message.reply("⬇️ Starting download...")
    asyncio.create_task(handle_leech_pipeline(client, cq.from_user.id, cq.message.chat.id, url, fmt_id, status_msg.id))
    await cq.answer()

# -----------------------
# Core Pipeline
# -----------------------
async def handle_leech_pipeline(client, user_id, chat_id, url, fmt_id, status_msg_id):
    tmpdir = Path(tempfile.mkdtemp())
    file_path = tmpdir / "video.mp4"

    async def progress_hook(d):
        if d["status"] == "downloading":
            try:
                progress = d.get("_percent_str", "0%")
                speed = d.get("_speed_str", "?")
                eta = d.get("_eta_str", "?")
                text = f"⬇️ Downloading: {progress} at {speed}, ETA {eta}"
                await client.edit_message_text(chat_id, status_msg_id, text)
            except Exception:
                pass

    ydl_opts = {
        "format": fmt_id,
        "outtmpl": str(file_path),
        "progress_hooks": [progress_hook],
        "merge_output_format": "mp4"
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        return await client.edit_message_text(chat_id, status_msg_id, f"❌ Download failed: {e}")

    parts = await split_file(file_path)
    total_parts = len(parts)

    async def upload_with_progress(part_path, part_index, total_parts):
        async def progress_cb(current, total, *args):
            try:
                percent = current * 100 / total
                await client.edit_message_text(chat_id, status_msg_id,
                    f"⬆️ Uploading {part_index}/{total_parts}: {percent:.1f}%")
            except Exception:
                pass

        await client.send_document(
            TG_CHAT,
            str(part_path),
            caption=f"Part {part_index}/{total_parts} - {part_path.name}",
            progress=progress_cb
        )

    try:
        for idx, p in enumerate(parts, 1):
            await upload_with_progress(p, idx, total_parts)
        await client.edit_message_text(chat_id, status_msg_id, "✅ All parts uploaded successfully!")
    except Exception as e:
        await client.send_message(TG_CHAT, f"❌ Upload failed: {e}")
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
