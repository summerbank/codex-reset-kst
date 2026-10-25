import asyncio
import hashlib
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv(Path(__file__).with_name(".env"))
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("codex-reset-monitor")
UTC = timezone.utc
KST = ZoneInfo("Asia/Seoul")
DATE_PATTERN = re.compile(r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?P<day>\d{1,2}),?\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*UTC\b", re.I)
MONTHS = {m.lower(): i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def required(key):
    val = os.getenv(key, "").strip()
    if not val:
        raise ValueError(f"Missing {key} in .env")
    return val


def parse_reset(text, posted_at):
    lower = text.lower()
    if "codex reset confirmed" in lower:
        return ("confirmed", None)
    if "codex reset" not in lower:
        return None
    status = "expected" if "codex reset expected" in lower else "announced" if "codex reset announced" in lower else None
    if status is None:
        return None
    match = DATE_PATTERN.search(text)
    if not match:
        LOG.warning("Reset post without recognized UTC date: %r", text[:140])
        return None
    month = MONTHS[match["month"][:3].lower()]
    posted_at = posted_at.astimezone(UTC)
    candidates = []
    for year in (posted_at.year - 1, posted_at.year, posted_at.year + 1):
        try:
            timestamp = datetime(year, month, int(match["day"]), int(match["hour"]), int(match["minute"]), tzinfo=UTC)
            candidates.append(timestamp)
        except ValueError:
            pass
    if not candidates:
        return None
    utc_time = min(candidates, key=lambda date: abs((date - posted_at).total_seconds()))
    if abs(utc_time - posted_at) > timedelta(days=180):
        LOG.warning("Ambiguous year/date in reset post: %r", text[:140])
        return None
    return (status, utc_time)


def render_notice(status, utc_time, source_link, edited=False):
    names = {"announced": "발표", "expected": "예상", "confirmed": "완료 확인"}
    header = "Codex 초기화 알림" + (" (수정됨)" if edited else "")
    lines = [header, f"상태: {names[status]}"]
    if utc_time:
        lines.append("한국시간: " + utc_time.astimezone(KST).strftime("%Y-%m-%d %H:%M KST"))
        lines.append("원문시간: " + utc_time.strftime("%Y-%m-%d %H:%M UTC"))
    else:
        lines.append("초기화 완료 공지가 게시됐습니다. (시간 표기 없음)")
    if source_link:
        lines.append("원문: " + source_link)
    return "\n".join(lines)


def message_link(username, chat_id, message_id):
    if username:
        return f"https://t.me/{username}/{message_id}"
    return f"https://t.me/c/{chat_id}/{message_id}"


async def main():
    api_id = int(required("TELEGRAM_API_ID"))
    api_hash = required("TELEGRAM_API_HASH")
    token = required("BOT_TOKEN")
    destination = required("BOT_CHAT_ID")
    source = required("SOURCE_CHANNEL")
    db = sqlite3.connect(DATA_DIR / "sent.sqlite3")
    db.execute("CREATE TABLE IF NOT EXISTS sent (channel_id INTEGER, message_id INTEGER, digest TEXT NOT NULL, PRIMARY KEY(channel_id, message_id))")
    db.commit()
    client = TelegramClient(str(DATA_DIR / "telegram_user"), api_id, api_hash)
    await client.start()
    try:
        entity = await client.get_entity(int(source) if source.lstrip("-").isdigit() else source)
        channel_id = entity.id
        username = getattr(entity, "username", None)
        LOG.info("Watching channel id=%s username=%s", channel_id, username)
        lock = asyncio.Lock()
        async with httpx.AsyncClient(timeout=20) as http:
            async def handle(event, edited=False):
                parsed = parse_reset(event.raw_text or "", event.message.date)
                if not parsed:
                    return
                status, utc_time = parsed
                digest = hashlib.sha256((status + "|" + (utc_time.isoformat() if utc_time else "")).encode()).hexdigest()
                async with lock:
                    row = db.execute("SELECT digest FROM sent WHERE channel_id=? AND message_id=?", (channel_id, event.message.id)).fetchone()
                    if row and row[0] == digest:
                        return
                    notice = render_notice(status, utc_time, message_link(username, channel_id, event.message.id), edited=bool(row or edited))
                    try:
                        response = await http.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": destination, "text": notice, "disable_web_page_preview": True})
                        response.raise_for_status()
                        result = response.json()
                        if not result.get("ok"):
                            raise RuntimeError(result.get("description", "sendMessage failed"))
                    except Exception:
                        LOG.exception("Notification failed for message %s; not marking as sent", event.message.id)
                        return
                    db.execute("INSERT OR REPLACE INTO sent VALUES (?, ?, ?)", (channel_id, event.message.id, digest))
                    db.commit()
                    LOG.info("Sent %s notification for message %s", status, event.message.id)

            client.add_event_handler(handle, events.NewMessage(chats=entity))
            async def on_edit(event):
                await handle(event, edited=True)
            client.add_event_handler(on_edit, events.MessageEdited(chats=entity))
            LOG.info("Listening for new/edited messages. No historical posts will be sent.")
            await client.run_until_disconnected()
    finally:
        db.close()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
