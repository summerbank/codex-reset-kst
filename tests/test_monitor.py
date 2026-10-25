import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from monitor import (Bot, BotError, Config, Store, deliver_one, parse_reset,
                     poll_registration, process_registration, reconcile, render_notice)

UTC = timezone.utc
POSTED = datetime(2026, 10, 15, tzinfo=UTC)
SECRET = 'a' * 32


def message(mid=11, text='🗓️ Codex reset announced by Oct 16, 05:00 UTC', edited=False):
    return SimpleNamespace(id=mid, raw_text=text, date=POSTED, edit_date=POSTED if edited else None)


def update(chat=123, text='/start ' + SECRET, kind='private', sender=None):
    return {'update_id': 50, 'message': {'text': text, 'chat': {'id': chat, 'type': kind},
                                       'from': {'id': chat if sender is None else sender}}}


class FakeBot:
    def __init__(self):
        self.sent = []
        self.fail = False

    async def send(self, chat, text):
        if self.fail:
            raise BotError(503)
        self.sent.append((chat, text))


class ParserTests(unittest.TestCase):
    def test_actual_channel_headlines(self):
        for headline, status in [
            ('🗓️ Codex reset announced by Oct 13, 07:00 UTC', 'announced'),
            ('⚠️ Possible Codex reset by Oct 16, 05:00 UTC', 'expected'),
            ('Codex reset expected by October 16, 05:00 UTC', 'expected'),
            ('✅ Codex reset confirmed', 'confirmed')]:
            with self.subTest(headline=headline):
                result = parse_reset(headline, POSTED)
                self.assertEqual(result[0], status)
        self.assertEqual(parse_reset('✅ Codex reset confirmed', POSTED), ('confirmed', None))

    def test_year_rollovers_and_kst(self):
        result = parse_reset('Codex reset announced by Jan 1, 00:15 UTC', datetime(2026, 12, 31, tzinfo=UTC))
        self.assertEqual(result[1].year, 2027)
        previous = parse_reset('Codex reset expected by Dec 31, 23:00 UTC', datetime(2027, 1, 1, tzinfo=UTC))
        self.assertEqual(previous[1].year, 2026)
        self.assertIn('2027-01-01 08:00 KST', render_notice(*previous, 'link'))

    def test_explicit_year_leap_date_and_invalid_dates(self):
        self.assertEqual(parse_reset('Codex reset announced by Feb 29, 2028, 07:00 UTC', POSTED)[1].year, 2028)
        for date in ['Feb 30, 07:00 UTC', 'Oct 16, 25:00 UTC', 'Oct 16, 07:99 UTC']:
            self.assertIsNone(parse_reset('Codex reset expected by ' + date, POSTED))
        self.assertIsNone(parse_reset('Codex reset expected by Apr 15, 07:00 UTC', POSTED))

    def test_headline_only_and_confirmed_date(self):
        self.assertIsNone(parse_reset('Unrelated\n> Codex reset confirmed', POSTED))
        parsed = parse_reset('Codex reset expected by Oct 16, 05:00 UTC\n> Codex reset confirmed', POSTED)
        self.assertEqual(parsed[0], 'expected')
        self.assertIsNotNone(parse_reset('Codex reset confirmed at Oct 16, 05:00 UTC', POSTED)[1])
        self.assertIsNone(parse_reset('Codex reset confirmed at Oct 99, 05:00 UTC', POSTED))
        self.assertIsNone(parse_reset('Codex reset announced\n> Oct 16, 05:00 UTC', POSTED))


class StateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'sent.sqlite3'
        self.store = Store(self.path)
        self.store.initialize_channel(1, 10)
        self.bot = FakeBot()

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def test_secure_registration_and_persistence(self):
        for bad in [update(text='/start'), update(text='/start wrong'), update(kind='group'),
                    update(sender=999), update(text='/start ' + '한글')]:
            await process_registration(self.store, self.bot, bad, SECRET)
        self.assertIsNone(self.store.owner())
        await process_registration(self.store, self.bot, update(), SECRET)
        self.assertEqual(self.store.owner(), 123)
        self.assertIn('등록 완료', self.bot.sent[-1][1])
        await process_registration(self.store, self.bot, update(chat=456), SECRET)
        self.assertEqual(self.store.owner(), 123)
        self.assertEqual(len(self.bot.sent), 1)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.owner(), 123)
        await process_registration(self.store, self.bot, update(text='/start'), SECRET)
        self.assertIn('등록된', self.bot.sent[-1][1])

    async def test_poll_offset_is_saved_after_handling(self):
        class PollBot(FakeBot):
            async def call(bot_self, method, **payload):
                if bot_self.sent:
                    self.assertEqual(payload['offset'], 51)
                    raise asyncio.CancelledError
                return [update()]
        bot = PollBot()
        with self.assertRaises(asyncio.CancelledError):
            await poll_registration(self.store, bot, SECRET)
        self.assertEqual(self.store.get('bot_offset'), '51')

    async def test_poll_confirmation_failure_does_not_ack_update(self):
        class PollBot(FakeBot):
            async def call(bot_self, method, **payload):
                return [update()]
        bot = PollBot()
        bot.fail = True
        async def cancel_sleep(seconds):
            self.assertEqual(seconds, 5)
            raise asyncio.CancelledError
        with patch('monitor.asyncio.sleep', cancel_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await poll_registration(self.store, bot, SECRET)
        self.assertIsNone(self.store.get('bot_offset'))
        self.assertEqual(self.store.owner(), 123)

    async def test_confirmation_failure_still_locks_owner(self):
        self.bot.fail = True
        with self.assertRaises(BotError):
            await process_registration(self.store, self.bot, update(), SECRET)
        self.assertEqual(self.store.owner(), 123)
        self.bot.fail = False
        await process_registration(self.store, self.bot, update(), SECRET)
        self.assertEqual(len(self.bot.sent), 1)

    async def test_optional_configured_owner_cannot_be_taken_over(self):
        self.store.configure_owner(789)
        await process_registration(self.store, self.bot, update(), SECRET)
        self.assertEqual(self.store.owner(), 789)
        with self.assertRaises(ValueError):
            self.store.configure_owner(123)

    async def test_retry_restart_and_semantic_edit_dedup(self):
        self.store.enqueue(1, 'codex_resets', message())
        self.assertFalse(await deliver_one(self.store, self.bot))  # waits for owner
        self.store.configure_owner(123)
        self.bot.fail = True
        with self.assertRaises(BotError):
            await deliver_one(self.store, self.bot)
        self.store.db.close()
        self.store = Store(self.path)
        self.bot.fail = False
        self.assertTrue(await deliver_one(self.store, self.bot))
        self.store.enqueue(1, 'codex_resets', message(text=message().raw_text + '\nNew quoted text'), edited=True)
        self.assertFalse(await deliver_one(self.store, self.bot))
        self.store.enqueue(1, 'codex_resets', message(text='✅ Codex reset confirmed'), edited=True)
        self.assertTrue(await deliver_one(self.store, self.bot))
        self.assertIn('수정됨', self.bot.sent[-1][1])
        self.store.db.close()
        self.store = Store(self.path)
        self.store.enqueue(1, 'codex_resets', message(text='✅ Codex reset confirmed'))
        self.assertFalse(await deliver_one(self.store, self.bot))
        self.assertEqual(len(self.bot.sent), 2)

    async def test_baseline_and_pending_retraction(self):
        self.store.enqueue(1, 'codex_resets', message(mid=10))
        self.assertIsNone(self.store.next_notice())
        self.store.enqueue(1, 'codex_resets', message())
        self.store.enqueue(1, 'codex_resets', message(text='Announcement withdrawn'), edited=True)
        self.assertIsNone(self.store.next_notice())

    async def test_edit_during_send_remains_pending(self):
        self.store.configure_owner(123)
        self.store.enqueue(1, 'codex_resets', message())
        original_send = self.bot.send
        async def send_and_edit(chat, text):
            self.store.enqueue(1, 'codex_resets', message(text='Codex reset confirmed'), edited=True)
            await original_send(chat, text)
        self.bot.send = send_and_edit
        await deliver_one(self.store, self.bot)
        self.assertIsNotNone(self.store.next_notice())
        self.bot.send = original_send
        await deliver_one(self.store, self.bot)
        self.assertEqual(len(self.bot.sent), 2)

    async def test_revert_during_send_is_not_lost(self):
        self.store.configure_owner(123)
        self.store.enqueue(1, 'codex_resets', message())
        await deliver_one(self.store, self.bot)
        self.store.enqueue(1, 'codex_resets', message(text='Codex reset confirmed'), edited=True)
        original_send = self.bot.send
        async def send_and_revert(chat, text):
            self.store.enqueue(1, 'codex_resets', message(), edited=True)
            await original_send(chat, text)
        self.bot.send = send_and_revert
        await deliver_one(self.store, self.bot)
        self.assertIsNotNone(self.store.next_notice())
        self.bot.send = original_send
        await deliver_one(self.store, self.bot)
        self.assertEqual(len(self.bot.sent), 3)

    async def test_history_recovery_does_not_skip_messages_on_live_event(self):
        items = [message(mid=11), message(mid=12)]
        class Client:
            async def iter_messages(_, entity, **kwargs):
                for item in items:
                    if 'min_id' not in kwargs or item.id > kwargs['min_id']:
                        yield item
        self.store.enqueue(1, 'codex_resets', message(mid=13))  # live events must not advance scan cursor
        await reconcile(Client(), self.store, SimpleNamespace(id=1, username='codex_resets'))
        self.assertEqual(self.store.cursor(1), 12)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 3)

    def test_existing_sent_schema_migrates(self):
        self.store.db.execute('INSERT INTO sent VALUES (1, 8, ?)', ('original',))
        self.store.db.commit()
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.db.execute('SELECT digest FROM sent').fetchone()[0], 'original')


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_errors_and_token_redaction(self):
        for code in [400, 401, 403, 409, 429, 500]:
            def handler(request):
                return httpx.Response(code, json={'ok': False, 'error_code': code,
                                                  'parameters': {'retry_after': 7}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                with self.assertRaises(BotError) as caught:
                    await Bot(http, 'TOP_SECRET').send(123, 'notice')
                self.assertEqual(caught.exception.retry_after, 7)
                self.assertEqual(caught.exception.fatal, code in (401, 409))
                self.assertNotIn('TOP_SECRET', str(caught.exception))

    async def test_transport_and_invalid_response_are_retryable(self):
        for mode in ['timeout', 'html']:
            def handler(request):
                if mode == 'timeout':
                    raise httpx.ReadTimeout('https://api.telegram.org/botTOP_SECRET/sendMessage')
                return httpx.Response(502, text='<html>Bad gateway</html>')
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                with self.assertRaises(BotError) as caught:
                    await Bot(http, 'TOP_SECRET').send(123, 'notice')
                self.assertFalse(caught.exception.fatal)
                self.assertNotIn('TOP_SECRET', str(caught.exception))

    async def test_success_payload(self):
        def handler(request):
            self.assertEqual(json.loads(request.content)['chat_id'], 123)
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 7}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            self.assertEqual((await Bot(http, 'dummy').send(123, 'notice'))['message_id'], 7)


class ConfigTests(unittest.TestCase):
    def test_env_validation(self):
        base = dict(TELEGRAM_API_ID='123', TELEGRAM_API_HASH='dummy', BOT_TOKEN='dummy',
                    SOURCE_CHANNEL='@codex_resets', BOT_REGISTRATION_SECRET=SECRET)
        with patch.dict(os.environ, base, clear=True):
            self.assertIsNone(Config.from_env().destination)
        for key, value in [('BOT_CHAT_ID', '@someone'), ('BOT_CHAT_ID', '-123'),
                           ('SOURCE_CHANNEL', 'codex_resets'), ('BOT_REGISTRATION_SECRET', 'short'),
                           ('TELEGRAM_API_ID', 'bad')]:
            with self.subTest(key=key), patch.dict(os.environ, {**base, key: value}, clear=True):
                with self.assertRaises(ValueError):
                    Config.from_env()
        with patch.dict(os.environ, {**base, 'BOT_CHAT_ID': '123', 'BOT_REGISTRATION_SECRET': ''}, clear=True):
            self.assertEqual(Config.from_env().destination, 123)


if __name__ == '__main__':
    unittest.main()
