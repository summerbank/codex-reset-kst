import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from monitor import BotError, Store, deliver_one, handle_deleted, reconcile, reconcile_deletions


def post(mid=11, text='Codex reset confirmed'):
    return SimpleNamespace(id=mid, raw_text=text, date=datetime(2026, 10, 28, tzinfo=timezone.utc), edit_date=None)


class DeletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'sent.sqlite3'
        self.store = Store(self.path)
        self.store.configure_owner(123)
        self.store.initialize_channel(1, 10)
        self.entity = SimpleNamespace(id=1, username='example_source')
        self.bot = SimpleNamespace(send=AsyncMock())
        self.client = SimpleNamespace(get_messages=AsyncMock(return_value=[None]))

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def sent(self, mid=11, text='Codex reset confirmed'):
        self.store.enqueue(1, 'example_source', post(mid, text))
        await deliver_one(self.store, self.bot)

    def delete(self, *ids):
        self.store.mark_deleted(1, 'example_source', ids or [11])

    async def test_sent_post_gets_one_correction_after_restart(self):
        await self.sent()
        self.delete()
        self.store.db.close()
        self.store = Store(self.path)
        await deliver_one(self.store, self.bot)
        target, notice = self.bot.send.call_args.args
        self.assertEqual(target, 123)
        self.assertIn('알림 정정', notice)
        self.assertIn('원문 삭제', notice)
        self.assertIn('https://t.me/example_source/11', notice)
        self.assertNotIn('초기화되지 않았습니다', notice)
        self.delete()
        self.store.db.close()
        self.store = Store(self.path)
        self.assertFalse(await deliver_one(self.store, self.bot))
        self.assertEqual(self.bot.send.await_count, 2)

    async def test_unsent_post_is_cancelled_without_correction(self):
        self.store.enqueue(1, 'example_source', post())
        self.delete()
        self.assertIsNone(self.store.next_notice())
        self.assertIsNone(self.store.next_correction())
        self.assertFalse(await deliver_one(self.store, self.bot))
        # A stale history response must not resurrect it.
        self.store.enqueue(1, 'example_source', post())
        self.assertIsNone(self.store.next_notice())

    async def test_delete_during_send_queues_correction_after_success(self):
        self.store.enqueue(1, 'example_source', post())
        async def delete_during_send(*args):
            self.delete()
        self.bot.send.side_effect = delete_during_send
        await deliver_one(self.store, self.bot)
        self.assertIsNotNone(self.store.next_correction())
        self.bot.send.side_effect = None
        await deliver_one(self.store, self.bot)
        self.assertIsNone(self.store.next_correction())

    async def test_failed_correction_persists_and_does_not_block_current_target(self):
        await self.sent()
        self.delete()
        self.bot.send.side_effect = BotError(403)
        with self.assertRaises(BotError):
            await deliver_one(self.store, self.bot)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.db.execute('SELECT corrected FROM deliveries').fetchone()[0], 0)
        self.assertIsNone(self.store.next_correction())  # retry is deferred
        self.store.put('destination_chat_id', -100123)
        self.store.enqueue(1, 'example_source', post(12))
        self.bot.send.side_effect = None
        self.assertTrue(await deliver_one(self.store, self.bot))
        self.assertEqual(self.bot.send.call_args.args[0], -100123)
        with self.store.db:
            self.store.db.execute('UPDATE deliveries SET retry_at=0')
        await deliver_one(self.store, self.bot)
        self.assertEqual(self.bot.send.call_args.args[0], 123)

    async def test_destination_change_during_send_keeps_original_receipt(self):
        self.store.enqueue(1, 'example_source', post())
        async def change_destination(*args):
            self.store.put('destination_chat_id', -100123)
        self.bot.send.side_effect = change_destination
        await deliver_one(self.store, self.bot)
        self.delete()
        self.bot.send.side_effect = None
        await deliver_one(self.store, self.bot)
        self.assertEqual(self.bot.send.call_args.args[0], 123)

    async def test_each_previous_recipient_gets_correction(self):
        await self.sent(text='Codex reset expected by Oct 28, 07:00 UTC')
        self.store.put('destination_chat_id', -100123)
        await self.sent()
        self.delete()
        await deliver_one(self.store, self.bot)
        await deliver_one(self.store, self.bot)
        self.assertEqual({call.args[0] for call in self.bot.send.await_args_list[-2:]}, {123, -100123})
        self.assertIsNone(self.store.next_correction())

    async def test_live_event_is_scoped_to_source_channel(self):
        await self.sent()
        for peer in (None, 1, -1000000000002):
            handle_deleted(self.store, self.entity, SimpleNamespace(chat_id=peer, deleted_ids=[11]))
        self.assertIsNone(self.store.next_correction())
        handle_deleted(self.store, self.entity, SimpleNamespace(chat_id=-1000000000001, deleted_ids=[11, 999]))
        self.assertIsNotNone(self.store.next_correction())
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM source_deleted').fetchone()[0], 1)

    async def test_recovery_needs_two_successful_id_lookups(self):
        await self.sent()
        await reconcile_deletions(self.client, self.store, self.entity)
        self.assertIsNone(self.store.next_correction())
        await reconcile_deletions(self.client, self.store, self.entity)
        self.client.get_messages.assert_awaited_with(self.entity, ids=[11])
        self.assertIsNotNone(self.store.next_correction())

    async def test_network_error_is_not_deletion_and_resets_confirmation(self):
        await self.sent()
        await reconcile_deletions(self.client, self.store, self.entity)
        self.client.get_messages.side_effect = OSError('offline')
        with self.assertRaises(OSError):
            await reconcile_deletions(self.client, self.store, self.entity)
        self.assertIsNone(self.store.next_correction())
        self.client.get_messages.side_effect = None
        await reconcile_deletions(self.client, self.store, self.entity)
        self.assertIsNone(self.store.next_correction())

    async def test_incomplete_or_mismatched_reply_never_means_deleted(self):
        await self.sent()
        for reply in ([], [post(999)], [None, None]):
            self.client.get_messages.return_value = reply
            await reconcile_deletions(self.client, self.store, self.entity)
            await reconcile_deletions(self.client, self.store, self.entity)
            self.assertIsNone(self.store.next_correction())

    async def test_reappearing_post_resets_confirmation(self):
        await self.sent()
        await reconcile_deletions(self.client, self.store, self.entity)
        self.client.get_messages.return_value = [post()]
        await reconcile_deletions(self.client, self.store, self.entity)
        self.client.get_messages.return_value = [None]
        await reconcile_deletions(self.client, self.store, self.entity)
        self.assertIsNone(self.store.next_correction())

    async def test_missing_latest_post_is_recovered_even_outside_returned_history(self):
        await self.sent(11)
        await self.sent(12)
        class Client:
            async def iter_messages(_, entity, **kwargs):
                yield post(11)
            async def get_messages(_, entity, ids):
                return [post(mid) if mid == 11 else None for mid in ids]
        await reconcile(Client(), self.store, self.entity)
        await reconcile(Client(), self.store, self.entity)
        self.assertEqual(self.store.next_correction()[1], 12)

    async def test_deleted_event_wins_over_inflight_history_reply(self):
        await self.sent()
        self.delete()
        self.store.enqueue(1, 'example_source', post(11, 'Codex reset expected by Oct 28, 07:00 UTC'))
        self.assertIsNone(self.store.next_notice())
        self.assertIsNotNone(self.store.next_correction())


class LegacyDeletionTests(unittest.TestCase):
    def test_legacy_sent_records_assigned_once_and_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'legacy.sqlite3'
            db = sqlite3.connect(path)
            db.executescript('''
                CREATE TABLE sent (channel_id INTEGER, message_id INTEGER, digest TEXT NOT NULL,
                                   PRIMARY KEY(channel_id, message_id));
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO sent VALUES (1, 11, 'legacy_digest');
                INSERT INTO settings VALUES ('owner_chat_id', '123');
                INSERT INTO settings VALUES ('destination_chat_id', '-100123');
            ''')
            db.close()
            store = Store(path)
            store.put('destination_chat_id', -100456)
            store.db.close()
            store = Store(path)
            store.mark_deleted(1, 'example_source', [11])
            self.assertEqual(store.next_correction()[2], -100123)
            self.assertEqual(store.db.execute('SELECT digest FROM sent').fetchone()[0], 'legacy_digest')
            store.db.close()
