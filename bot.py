import os
import re
from urllib.parse import quote
from aiohttp import web
from pyrogram import Client, filters

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BIN_CHANNEL = int(os.environ.get("BIN_CHANNEL", 0))
FQDN = os.environ.get("FQDN", "").rstrip("/")
PORT = int(os.environ.get("PORT", 8080))

CHUNK_SIZE = 1024 * 1024  # 1MB chunks

bot = Client(
    "imdex_stream_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

def get_media_info(msg):
    media = msg.document or msg.video or msg.audio
    if not media:
        return None, None, None
    file_name = getattr(media, "file_name", None) or f"IMDEX_Movie_{msg.id}.mp4"
    file_size = getattr(media, "file_size", 0)
    mime_type = getattr(media, "mime_type", None) or "application/octet-stream"
    return file_name, file_size, mime_type

# Telegram Bot Handler: Channel එකේ ෆයිල් එකක් Forward කළ විට Link එක දීම
@bot.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def handle_file(client, message):
    # දැනටමත් BIN_CHANNEL එකේ ඇති ෆයිල් එකක් Forward කළේ නම් එම ID එකම ගනී
    if (
        message.forward_from_chat
        and message.forward_from_chat.id == BIN_CHANNEL
        and message.forward_from_message_id
    ):
        msg_id = message.forward_from_message_id
    else:
        copied = await message.copy(BIN_CHANNEL)
        msg_id = copied.id

    file_name, _, _ = get_media_info(message)
    safe_name = quote(file_name)
    base_url = FQDN if FQDN else f"http://localhost:{PORT}"
    direct_link = f"{base_url}/dl/{msg_id}/{safe_name}"

    await message.reply_text(
        f"✅ **Direct Download Link Ready!**\n\n"
        f"📁 **File:** `{file_name}`\n\n"
        f"🔗 **Direct Link:**\n`{direct_link}`",
        quote=True,
    )

# Health Check Route
async def home_handler(request):
    return web.Response(text="IMDEX Direct Download Server is Running!")

# Direct File Stream & Download Route (Chrome + IDM Resume Support)
async def download_handler(request):
    try:
        msg_id = int(request.match_info["msg_id"])
    except ValueError:
        return web.Response(status=400, text="Invalid Message ID")

    msg = await bot.get_messages(BIN_CHANNEL, msg_id)
    if not msg or msg.empty:
        return web.Response(status=404, text="File not found in channel")

    file_name, file_size, mime_type = get_media_info(msg)
    if not file_size:
        return web.Response(status=404, text="No downloadable media found")

    range_header = request.headers.get("Range")
    start = 0
    end = file_size - 1
    status = 200

    if range_header:
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            start = int(match.group(1))
            if match.group(2):
                end = min(int(match.group(2)), file_size - 1)
            status = 206

    if start > end or start >= file_size:
        return web.Response(status=416, headers={"Content-Range": f"bytes */{file_size}"})

    content_length = end - start + 1
    headers = {
        "Content-Type": mime_type,
        "Content-Disposition": f'attachment; filename="{file_name}"',
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    resp = web.StreamResponse(status=status, headers=headers)
    await resp.prepare(request)

    start_chunk = start // CHUNK_SIZE
    end_chunk = end // CHUNK_SIZE
    chunk_count = end_chunk - start_chunk + 1
    skip_bytes = start % CHUNK_SIZE
    remaining = content_length

    try:
        async for chunk in bot.stream_media(msg, offset=start_chunk, limit=chunk_count):
            if skip_bytes > 0:
                chunk = chunk[skip_bytes:]
                skip_bytes = 0
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            await resp.write(chunk)
            remaining -= len(chunk)
            if remaining <= 0:
                break
    except Exception:
        pass

    return resp

async def main():
    await bot.start()
    app = web.Application()
    app.router.add_get("/", home_handler)
    app.router.add_get("/dl/{msg_id}/{file_name}", download_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Server started on port {PORT}")

    import asyncio
    await asyncio.Event().wait()

if __name__ == "__main__":
    bot.run(main())
