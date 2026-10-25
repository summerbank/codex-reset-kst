"""Codex reset channel watcher; all durable state lives in DATA_DIR."""
import argparse
import asyncio
import fcntl
import hashlib
import hmac
import logging
import os
import re
import secrets
import signal
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, errors, events

UTC = timezone.utc
KST = ZoneInfo("Asia/Seoul")
LOG = logging.getLogger("codex-reset-monitor")
DATE_PATTERN = re.compile(
    r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(?P<day>\d{1,2})(?:,?\s+(?P<year>\d{4}))?,?\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*UTC\b", re.I)
STATUS_PATTERN = re.compile(r"\b(?:possible\s+codex\s+reset|codex\s+reset\s+(announced|expected|confirmed))\b", re.I)
MONTHS = {m.lower(): i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def parse_reset(text, posted_at):
    # Only the headline is authoritative; quoted posts may mention other states/dates.
    headline = next((line.strip() for line in text.splitlines() if line.strip()), "")
    status_match = STATUS_PATTERN.search(headline)
    if not status_match:
        return None
    status = (status_match[1] or "expected").lower()
    match = DATE_PATTERN.search(headline)
    if not match:
        if status == "confirmed":
            return status, None
        LOG.warning("Reset headline has no recognized UTC date")
        return None
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)
    posted_at = posted_at.astimezone(UTC)
    years = [int(match["year"])] if match["year"] else range(posted_at.year - 1, posted_at.year + 2)
    candidates = []
    for year in years:
        try:
            candidates.append(datetime(year, MONTHS[match["month"][:3].lower()],
                                       int(match["day"]), int(match["hour"]), int(match["minute"]), tzinfo=UTC))
        except ValueError:
            continue
    if not candidates:
        LOG.warning("Reset headline has an invalid UTC date")
        return None
    utc_time = min(candidates, key=lambda date: abs(date - posted_at))
    if not match["year"] and abs(utc_time - posted_at) > timedelta(days=180):
        LOG.warning("Reset headline has an ambiguous year")
        return None
    return status, utc_time


def render_notice(status, utc_time, source_link, edited=False):
    names = {"announced": "발표", "expected": "예상", "confirmed": "완료 확인"}
    lines = ["Codex 초기화 알림" + (" (수정됨)" if edited else ""), f"상태: {names[status]}"]
    if utc_time:
        lines += ["한국시간: " + utc_time.astimezone(KST).strftime("%Y-%m-%d %H:%M KST"),
                  "원문시간: " + utc_time.strftime("%Y-%m-%d %H:%M UTC")]
    else:
        lines.append("초기화 완료 공지가 게시됐습니다. (시간 표기 없음)")
    if source_link:
        lines.append("원문: " + source_link)
    return "\n".join(lines)


def message_link(username, chat_id, message_id):
    return f"https://t.me/{username}/{message_id}" if username else f"https://t.me/c/{chat_id}/{message_id}"


@dataclass(repr=False)
class Config:
    api_id: int
    api_hash: str
    token: str
    source: str
    destination: int | None
    registration_secret: str
    data_dir: Path

    @classmethod
    def from_env(cls):
        def required(key):
            value = os.getenv(key, "").strip()
            if not value:
                raise ValueError(f".env에 {key}를 설정하세요.")
            return value
        api_id = required("TELEGRAM_API_ID")
        if not api_id.isdecimal() or int(api_id) <= 0:
            raise ValueError("TELEGRAM_API_ID는 양의 정수여야 합니다.")
        destination = os.getenv("BOT_CHAT_ID", "").strip()
        if destination and (not destination.isdecimal() or int(destination) <= 0):
            raise ValueError("BOT_CHAT_ID는 개인 채팅의 양의 숫자 ID입니다. @username은 사용할 수 없습니다.")
        source = required("SOURCE_CHANNEL")
        if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", source):
            raise ValueError("SOURCE_CHANNEL에는 @codex_resets처럼 @채널사용자명을 입력하세요.")
        secret = os.getenv("BOT_REGISTRATION_SECRET", "").strip()
        if not destination and not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", secret):
            raise ValueError("자동 등록에는 24~64자의 BOT_REGISTRATION_SECRET이 필요합니다. --generate-registration-secret을 사용하세요.")
        return cls(int(api_id), required("TELEGRAM_API_HASH"), required("BOT_TOKEN"), source,
                   int(destination) if destination else None, secret,
                   Path(os.getenv("DATA_DIR", str(Path(__file__).parent / "data"))))


class Store:
    def __init__(self, path):
        self.sending = None
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sent (
                channel_id INTEGER, message_id INTEGER, digest TEXT NOT NULL,
                PRIMARY KEY(channel_id, message_id));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS watched (
                channel_id INTEGER PRIMARY KEY, floor INTEGER NOT NULL, cursor INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                channel_id INTEGER, message_id INTEGER, digest TEXT NOT NULL, notice TEXT NOT NULL,
                PRIMARY KEY(channel_id, message_id));
        """)

    def get(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, str(value)))

    def owner(self):
        value = self.get("owner_chat_id")
        return int(value) if value else None

    def configure_owner(self, destination):
        if destination:
            if self.owner() and self.owner() != destination:
                raise ValueError("BOT_CHAT_ID가 저장된 소유자와 다릅니다. README의 소유자 변경 절차를 확인하세요.")
            self.put("owner_chat_id", destination)

    def initialize_channel(self, channel_id, latest_id):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO watched VALUES (?, ?, ?)", (channel_id, latest_id, latest_id))

    def cursor(self, channel_id):
        row = self.db.execute("SELECT cursor FROM watched WHERE channel_id=?", (channel_id,)).fetchone()
        return row[0] if row else None

    def advance(self, channel_id, message_id):
        with self.db:
            self.db.execute("UPDATE watched SET cursor=MAX(cursor, ?) WHERE channel_id=?", (message_id, channel_id))

    def enqueue(self, channel_id, username, message, edited=False):
        row = self.db.execute("SELECT floor FROM watched WHERE channel_id=?", (channel_id,)).fetchone()
        sent = self.db.execute("SELECT digest FROM sent WHERE channel_id=? AND message_id=?",
                               (channel_id, message.id)).fetchone()
        if not row or (message.id <= row[0] and not sent):
            return
        parsed = parse_reset(message.raw_text or "", message.date)
        with self.db:
            if not parsed:
                # An edit can retract a pending reset announcement.
                self.db.execute("DELETE FROM outbox WHERE channel_id=? AND message_id=?", (channel_id, message.id))
                return
            status, utc_time = parsed
            digest = hashlib.sha256((status + "|" + (utc_time.isoformat() if utc_time else "")).encode()).hexdigest()
            if sent and sent[0] == digest and self.sending != (channel_id, message.id):
                self.db.execute("DELETE FROM outbox WHERE channel_id=? AND message_id=?", (channel_id, message.id))
                return
            notice = render_notice(status, utc_time, message_link(username, channel_id, message.id), bool(sent or edited))
            self.db.execute("INSERT OR REPLACE INTO outbox VALUES (?, ?, ?, ?)", (channel_id, message.id, digest, notice))

    def next_notice(self):
        return self.db.execute("SELECT channel_id, message_id, digest, notice FROM outbox ORDER BY channel_id, message_id LIMIT 1").fetchone()

    def delivered(self, channel_id, message_id, digest):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sent VALUES (?, ?, ?)", (channel_id, message_id, digest))
            self.db.execute("DELETE FROM outbox WHERE channel_id=? AND message_id=? AND digest=?", (channel_id, message_id, digest))


class BotError(Exception):
    def __init__(self, code=0, retry_after=5):
        # Never include response bodies, URLs, or httpx exceptions (they contain BOT_TOKEN).
        super().__init__(f"Telegram Bot API 오류 (code={code})")
        self.code = code
        self.retry_after = max(1, retry_after)

    @property
    def fatal(self):
        return self.code in (401, 404, 409)


class Bot:
    def __init__(self, http, token):
        self.http = http
        self.url = f"https://api.telegram.org/bot{token}/"

    async def call(self, method, **payload):
        try:
            response = await self.http.post(self.url + method, json=payload)
            result = response.json()
        except (httpx.HTTPError, ValueError):
            raise BotError() from None
        if not response.is_success or not result.get("ok"):
            raise BotError(result.get("error_code", response.status_code),
                           result.get("parameters", {}).get("retry_after", 5))
        return result["result"]

    async def send(self, chat_id, text):
        return await self.call("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True)


async def process_registration(store, bot, update, secret):
    message = update.get("message", {})
    chat = message.get("chat", {})
    sender = message.get("from", {})
    chat_id = chat.get("id")
    if chat.get("type") != "private" or chat_id != sender.get("id") or sender.get("is_bot"):
        return
    parts = message.get("text", "").split(maxsplit=1)
    if not parts:
        return
    command = parts[0].lower()
    owner = store.owner()
    if command in ("/ping", "/status"):
        if owner == chat_id:
            await bot.send(chat_id, "🏓 Pong! 모니터 프로세스가 실행 중입니다.\n응답 시각: "
                           + datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"))
        return
    if command != "/start":
        return
    if owner:
        if owner == chat_id:
            await bot.send(chat_id, "등록된 개인 채팅입니다. Codex 초기화 알림을 이곳으로 보냅니다.")
        return
    supplied = parts[1] if len(parts) > 1 else ""
    if not secret or not hmac.compare_digest(supplied.encode(), secret.encode()):
        # Do not expose whether a code was close/correct to an unregistered caller.
        return
    store.put("owner_chat_id", chat_id)
    await bot.send(chat_id, "개인 채팅 등록 완료! Codex 초기화 알림을 이곳으로 보냅니다.")
    LOG.info("Personal notification chat registered")


async def poll_registration(store, bot, secret):
    while True:
        try:
            updates = await bot.call("getUpdates", offset=int(store.get("bot_offset") or 0), timeout=25,
                                     allowed_updates=["message"])
            for update in updates:
                await process_registration(store, bot, update, secret)
                store.put("bot_offset", update["update_id"] + 1)
        except BotError as exc:
            if exc.fatal:
                LOG.error("Bot API stopped (code=%s); check token and duplicate polling", exc.code)
                raise
            LOG.warning("Bot update polling failed (code=%s); retrying", exc.code)
            await asyncio.sleep(exc.retry_after)


async def deliver_one(store, bot):
    row = store.next_notice() if store.owner() else None
    if not row:
        return False
    channel_id, message_id, digest, notice = row
    store.sending = (channel_id, message_id)
    try:
        await bot.send(store.owner(), notice)
        store.delivered(channel_id, message_id, digest)
    finally:
        store.sending = None
    LOG.info("Notification sent for source message %s", message_id)
    return True


async def send_pending(store, bot):
    while True:
        try:
            sent = await deliver_one(store, bot)
            await asyncio.sleep(1 if sent else 2)
        except BotError as exc:
            if exc.fatal:
                LOG.error("Bot API stopped (code=%s); check token and duplicate polling", exc.code)
                raise
            LOG.warning("Notification retained for retry (code=%s). Check /start and bot block settings.", exc.code)
            await asyncio.sleep(exc.retry_after)


async def reconcile(client, store, entity):
    channel_id = entity.id
    username = getattr(entity, "username", None)
    # Reverse order lets us commit a contiguous cursor even if a scan is interrupted.
    async for message in client.iter_messages(entity, min_id=store.cursor(channel_id), reverse=True):
        store.enqueue(channel_id, username, message, edited=bool(message.edit_date))
        store.advance(channel_id, message.id)
    # Telegram history is ordered by original ID, not edit time. Bound edit recovery explicitly.
    async for message in client.iter_messages(entity, limit=100):
        store.enqueue(channel_id, username, message, edited=bool(message.edit_date))


async def scan_channel(client, store, entity):
    while True:
        try:
            await reconcile(client, store, entity)
        except errors.FloodWaitError as exc:
            LOG.warning("Channel scan rate limited; waiting %s seconds", exc.seconds)
            await asyncio.sleep(exc.seconds)
        except (OSError, asyncio.TimeoutError, errors.ServerError):
            LOG.warning("Channel scan failed; retrying in 60 seconds")
        await asyncio.sleep(60)


async def watch_disconnect(client):
    await client.disconnected
    raise RuntimeError("Telegram 사용자 연결이 종료됐습니다. 세션/네트워크와 로그를 확인하세요.")


class CheckFailure(Exception):
    """Only fixed, safe diagnostic messages may be placed in this exception."""


async def check_connections(config, store, client, bot, report=print):
    results = []

    def record(label, ok, detail):
        results.append((label, ok, detail))
        report(f"[{'정상' if ok else '실패'}] {label}: {detail}")

    async def step(label, operation, hint):
        try:
            detail = await asyncio.wait_for(operation(), timeout=20)
        except CheckFailure as exc:
            record(label, False, str(exc))
        except BotError as exc:
            record(label, False, f"Bot API code={exc.code}. {hint}")
        except asyncio.TimeoutError:
            record(label, False, f"20초 내 응답 없음. {hint}")
        except Exception as exc:
            # Exception text and request URLs may contain credentials.
            record(label, False, f"{type(exc).__name__}. {hint}")
        else:
            record(label, True, detail)
            return True
        return False

    async def database():
        if store.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise CheckFailure("SQLite 무결성 확인 실패. data/ 백업을 확인하세요.")
        count = store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        return f"SQLite 정상, 전송 대기 {count}건"

    async def user_session():
        await client.connect()
        if not await client.is_user_authorized():
            raise CheckFailure("최초 인증 필요: docker compose run --rm monitor python monitor.py --login")
        account = await client.get_me()
        if not account or account.bot:
            raise CheckFailure("사용자 계정 세션이 필요합니다. --login으로 사용자 계정을 인증하세요.")
        return "사용자 계정 인증 및 Telegram 응답 확인"

    async def channel():
        entity = await client.get_entity(config.source)
        if not getattr(entity, "broadcast", False):
            raise CheckFailure("SOURCE_CHANNEL이 방송 채널이 아닙니다. @채널사용자명을 확인하세요.")
        messages = await client.get_messages(entity, limit=1)
        if getattr(entity, "left", False):
            raise CheckFailure("채널 기록 접근 가능, 채널 미가입. 로그인한 사용자 계정으로 채널에 가입하세요.")
        return "채널 가입 및 최신 기록 조회 확인" if messages else "채널 가입 및 기록 조회 확인 (게시물 없음)"

    async def bot_identity():
        identity = await bot.call("getMe")
        if store.get("bot_id") and store.get("bot_id") != str(identity["id"]):
            raise CheckFailure("저장된 봇과 BOT_TOKEN의 봇이 다릅니다. README의 봇 변경 절차를 확인하세요.")
        return "봇 토큰 인증 및 HTTPS 응답 확인"

    async def webhook():
        if (await bot.call("getWebhookInfo")).get("url"):
            raise CheckFailure("webhook이 설정되어 polling을 사용할 수 없습니다. 전용 봇을 사용하세요.")
        return "webhook 없음 (다른 polling 프로세스의 존재는 검사하지 않음)"

    async def destination():
        if not store.owner():
            raise CheckFailure("개인 채팅 미등록. 모니터 실행 후 봇에 /start 등록코드 를 보내세요.")
        chat = await bot.call("getChat", chat_id=store.owner())
        if chat.get("type") != "private" or chat.get("id") != store.owner():
            raise CheckFailure("등록된 대상이 올바른 개인 채팅이 아닙니다.")
        return "등록된 개인 채팅 조회 성공 (실제 수신은 --test-notification으로 확인)"

    report("연결 진단: " + datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"))
    await step("저장소", database, "data/ 권한과 디스크 상태를 확인하세요.")
    if await step("사용자 세션", user_session, "NAS 네트워크와 사용자 인증 상태를 확인하세요."):
        await step("원본 채널", channel, "SOURCE_CHANNEL 및 사용자 계정의 채널 접근 권한을 확인하세요.")
    else:
        record("원본 채널", False, "사용자 세션 실패로 검사 생략")
    if await step("알림 봇", bot_identity, "BOT_TOKEN과 NAS의 HTTPS 연결을 확인하세요."):
        await step("봇 수신 설정", webhook, "봇의 webhook 설정과 네트워크를 확인하세요.")
        await step("개인 채팅", destination, "봇의 개인 대화에서 /start 및 등록 상태를 확인하세요.")
    else:
        record("봇 수신 설정", False, "봇 인증 실패로 검사 생략")
        record("개인 채팅", False, "봇 인증 실패로 검사 생략")
    ok = all(item[1] for item in results)
    report("진단 완료: " + ("모든 항목 정상" if ok else "실패 항목의 안내를 확인하세요."))
    return ok


async def diagnose(config, store):
    client = TelegramClient(str(config.data_dir / "telegram_user"), config.api_id, config.api_hash,
                            connection_retries=0, request_retries=0, flood_sleep_threshold=0)
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            return await check_connections(config, store, client, Bot(http, config.token),
                                           report=lambda text: print(text, flush=True))
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5)
        except Exception:
            LOG.warning("Diagnostic session cleanup failed; process will exit")


async def run(config, store, authenticate=False):
    client = TelegramClient(str(config.data_dir / "telegram_user"), config.api_id, config.api_hash,
                            auto_reconnect=True, connection_retries=5)
    try:
        if authenticate:
            await client.start()
            LOG.info("Telegram user authentication saved. Start the monitor with docker compose up -d")
            return
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("Telegram 최초 인증이 필요합니다: docker compose run --rm monitor python monitor.py --login")
        entity = await client.get_entity(config.source)
        if store.cursor(entity.id) is None:
            latest = await client.get_messages(entity, limit=1)
            store.initialize_channel(entity.id, latest[0].id if latest else 0)
        async def handle(event):
            store.enqueue(entity.id, getattr(entity, "username", None), event.message,
                          edited=bool(event.message.edit_date))
        client.add_event_handler(handle, events.NewMessage(chats=entity))
        client.add_event_handler(handle, events.MessageEdited(chats=entity))
        async with httpx.AsyncClient(timeout=40) as http:
            bot = Bot(http, config.token)
            # These calls fail fast on invalid credentials/webhook conflicts, retry transient errors.
            while True:
                try:
                    identity = await bot.call("getMe")
                    if store.get("bot_id") and store.get("bot_id") != str(identity["id"]):
                        raise ValueError("봇이 변경됐습니다. 별도 DATA_DIR을 사용하세요 (README 참고).")
                    store.put("bot_id", identity["id"])
                    if (await bot.call("getWebhookInfo")).get("url"):
                        raise ValueError("봇에 webhook이 설정돼 있습니다. 전용 BotFather 봇을 사용하세요.")
                    break
                except BotError as exc:
                    if exc.fatal:
                        raise
                    LOG.warning("Bot startup check failed (code=%s); retrying", exc.code)
                    await asyncio.sleep(exc.retry_after)
            LOG.info("Watching %s; durable retry and 60-second history reconciliation enabled", config.source)
            if not store.owner():
                LOG.info("Waiting for private registration: send /start followed by BOT_REGISTRATION_SECRET to your bot")
            async with asyncio.TaskGroup() as group:
                group.create_task(poll_registration(store, bot, config.registration_secret))
                group.create_task(send_pending(store, bot))
                group.create_task(scan_channel(client, store, entity))
                group.create_task(watch_disconnect(client))
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="메시지 전송 없이 연결 상태 진단")
    mode.add_argument("--login", action="store_true", help="Telegram 사용자 최초 인증만 실행")
    mode.add_argument("--test-notification", action="store_true", help="등록된 채팅에 테스트 알림 전송")
    mode.add_argument("--generate-registration-secret", action="store_true", help="랜덤 등록코드 생성")
    args = parser.parse_args()
    if args.generate_registration_secret:
        print(secrets.token_urlsafe(32))
        return
    load_dotenv(Path(__file__).with_name(".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    os.umask(0o077)
    try:
        config = Config.from_env()
        config.data_dir.mkdir(parents=True, exist_ok=True)
        with (config.data_dir / "monitor.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("동일 DATA_DIR의 프로세스가 실행 중입니다. 먼저 docker compose stop 하세요.") from None
            store = Store(config.data_dir / "sent.sqlite3")
            try:
                store.configure_owner(config.destination)
                async def entry():
                    task = asyncio.current_task()
                    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
                    if args.check:
                        if not await diagnose(config, store):
                            raise SystemExit(1)
                    elif args.test_notification:
                        if not store.owner():
                            raise ValueError("먼저 봇에서 /start 등록코드 로 개인 채팅을 등록하세요.")
                        async with httpx.AsyncClient(timeout=40) as http:
                            await Bot(http, config.token).send(store.owner(),
                                "Codex 초기화 테스트 알림\n한국시간: " + datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"))
                        LOG.info("Test notification sent")
                    else:
                        await run(config, store, args.login)
                asyncio.run(entry())
            finally:
                store.db.close()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except (ValueError, BotError) as exc:
        LOG.error("%s", exc)
        sys.exit(1)
    except BaseExceptionGroup as exc:
        # Avoid tracebacks containing request URLs or user-supplied values.
        LOG.error("Background task stopped (%s). Check bot token, duplicate polling, Telegram session and network.",
                  ", ".join(type(error).__name__ for error in exc.exceptions))
        sys.exit(1)
    except Exception as exc:
        LOG.error("Monitor stopped (%s). Check Telegram credentials, source access and network.", type(exc).__name__)
        sys.exit(1)


if __name__ == "__main__":
    main()
