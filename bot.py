import os
import re
import asyncio
import traceback
from urllib.parse import quote
from aiohttp import web
from pyrogram import Client, filters

API_ID = int(os.environ.get("API_ID", "0").strip())
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BIN_CHANNEL = int(os.environ.get("BIN_CHANNEL", "0").strip())
ACCESS_HASH = int(os.environ.get("ACCESS_HASH", "0").strip() or "0")
FQDN = os.environ.get("FQDN", "").strip().rstrip("/")
PORT = int(os.environ.get("PORT", "8080").strip())

CHUNK_SIZE = 1024 * 1024  # 1MB chunks

bot = Client(
    "imdex_stream_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

# Server Wake-up වූ විට Private Channel එක මතකයට ලබාගැනීම
async def ensure_channel_cached():
    try:
        await bot.get_chat(BIN_CHANNEL)
        return
    except Exception:
        pass

    if ACCESS_HASH:
        await bot.storage.update_peers([(BIN_CHANNEL, ACCESS_HASH, "channel", None, None)])
        await bot.get_chat(BIN_CHANNEL)
        return

    raise RuntimeError(
        "Channel not cached yet. Telegram Bot ට ඔබේ Private Channel එකෙන් ෆයිල් එකක් Forward කර "
        "ලැබෙන ACCESS_HASH අංකය Render Environment Variables වලට එකතු කරන්න."
    )

def get_media_info(msg):
    media = msg.document or msg.video or msg.audio
    if not media:
        return None, None, None
    file_name = getattr(media, "file_name", None) or f"IMDEX_Movie_{msg.id}.mp4"
    file_size = getattr(media, "file_size", 0)
    mime_type = getattr(media, "mime_type", None) or "application/octet-stream"
    return file_name, file_size, mime_type

# /start command එකට පිළිතුරු දීම
@bot.on_message(filters.private & filters.command("start"))
async def start_cmd(client, message):
    await message.reply_text(
        "👋 **IMDEX Direct Download Bot Online!**\n\n"
        "ඔබගේ Private Channel එකේ ඇති Movie හෝ Episode ෆයිල් එකක් මට **Forward** කරන්න.",
        quote=True,
    )

# ෆයිල් එකක් Forward කළ විට Direct Link එක සෑදීම
@bot.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def handle_file(client, message):
    status_msg = await message.reply_text("⏳ Direct Link එක සකසමින් පවතී...", quote=True)
    try:
        if (
            message.forward_from_chat
            and message.forward_from_chat.id == BIN_CHANNEL
            and message.forward_from_message_id
        ):
            msg_id = message.forward_from_message_id
        else:
            await ensure_channel_cached()
            copied = await message.copy(BIN_CHANNEL)
            msg_id = copied.id

        peer = await client.resolve_peer(BIN_CHANNEL)
        ch_hash = getattr(peer, "access_hash", 0)

        file_name, _, _ = get_media_info(message)
        safe_name = quote(file_name)
        base_url = FQDN if FQDN else "https://imdex-stream-bot.onrender.com"
        direct_link = f"{base_url}/dl/{msg_id}/{safe_name}"

        hash_note = ""
        if not ACCESS_HASH and ch_hash:
            hash_note = (
                f"\n\n🔑 **Render සඳහා `ACCESS_HASH` (එක් වරක් පමණක් Save කරන්න):**\n"
                f"`{ch_hash}`\n"
                f"*(Server එක Sleep වී ආපසු Wake වෙද්දී Error නොඑන්න, ඉහත අංකය Render Environment Variables වල `ACCESS_HASH` ලෙස Save කරන්න)*"
            )

        await status_msg.edit_text(
            f"✅ **Direct Download Link Ready!**\n\n"
            f"📁 **File:** `{file_name}`\n\n"
            f"🔗 **Direct Link:**\n`{direct_link}`"
            f"{hash_note}"
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ **Error:**\n`{e}`")

async def home_handler(request):
    return web.Response(text="IMDEX Direct Download Server is Running!")

async def download_handler(request):
    try:
        try:
            msg_id = int(request.match_info["msg_id"])
        except ValueError:
            return web.Response(status=400, text="Invalid Message ID")

        await ensure_channel_cached()

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
        ascii_name = file_name.encode("ascii", "ignore").decode("ascii") or f"IMDEX_Movie_{msg_id}.mp4"
        quoted_name = quote(file_name)

        headers = {
            "Content-Type": mime_type,
            "Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted_name}',
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

    except Exception as e:
        traceback.print_exc()
        return web.Response(status=500, text=f"Download Error: {e}")

async def main():
    await bot.start()
    me = await bot.get_me()
    print(f"Bot started as @{me.username}")

    try:
        await ensure_channel_cached()
        print("BIN_CHANNEL cached successfully.")
    except Exception as e:
        print(f"Info: {e}")

    app = web.Application()
    app.router.add_get("/", home_handler)
    app.router.add_get("/dl/{msg_id}/{file_name}", download_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Server started on port {PORT}")

    await asyncio.Event().wait()

if __name__ == "__main__":
    bot.run(main())
