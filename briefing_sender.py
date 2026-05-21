import asyncio, logging, os
import httpx

log = logging.getLogger(__name__)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
MAX_LEN = 4096

async def send_message_http(text, parse_mode="HTML", bot_token=None, chat_id=None):
    token = bot_token or BOT_TOKEN
    cid = chat_id or CHAT_ID
    if not token or not cid:
        log.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = []
    remaining = text
    while len(remaining) > MAX_LEN:
        split_at = remaining.rfind("\n", 0, MAX_LEN)
        if split_at == -1:
            split_at = MAX_LEN
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    chunks.append(remaining)
    async with httpx.AsyncClient(timeout=30) as client:
        for idx, chunk in enumerate(chunks, 1):
            try:
                resp = await client.post(url, json={"chat_id": cid, "text": chunk, "parse_mode": parse_mode})
                resp.raise_for_status()
                log.info(f"Chunk {idx}/{len(chunks)} sent ✓")
            except Exception as e:
                log.error(f"Send error: {e}")
                return False
    return True

async def send_error_alert(context, error):
    msg = f"⚠️ <b>StockSage Error — {context}</b>\n\n<code>{type(error).__name__}: {str(error)[:300]}</code>"
    await send_message_http(msg)
