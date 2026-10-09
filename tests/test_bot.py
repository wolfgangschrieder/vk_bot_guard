import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import bot
import config
import database as db


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'test.db')
        self.conn = db.connect(self.path)
        bot.ACTIVE_CONN = self.conn
        bot.STAT_ACCESS_CACHE.clear()
        self.vk = MagicMock()
        self.vk.messages.send.return_value = 100
        self.vk.messages.delete.return_value = {'1': 1}
        self.vk.messages.changeConversationMemberRestrictions.return_value = 1
        self.vk.users.get.side_effect = self.profiles
        self.now = int(datetime(2026, 10, 9, 14, tzinfo=config.CHAT_TZ).timestamp())
        self.clock = patch('time.time', return_value=self.now)
        self.clock.start()
        self.admins = patch.object(config, 'ADMIN_IDS', {99})
        self.admins.start()
        self.stat_ids = patch.object(config, 'STAT_ALLOWED_IDS', set())
        self.stat_ids.start()

    def tearDown(self):
        self.stat_ids.stop()
        self.admins.stop()
        self.clock.stop()
        bot.ACTIVE_CONN = None
        self.conn.close()
        self.tmp.cleanup()

    def profiles(self, user_ids):
        aliases = {'kenaya': 10, 'piterparker34': 20, 'id1122341522': 1122341522}
        return [{'id': aliases.get(uid, int(uid) if uid.isdigit() else 0),
                 'first_name': 'Test', 'last_name': 'User'} for uid in str(user_ids).split(',')]

    def message(self, text='hello', uid=1, mid=1, peer=None, **extra):
        return {'id': mid, 'peer_id': peer or config.CHAT_PEER_ID, 'from_id': uid,
                'text': text, 'date': self.now, **extra}

    def queued(self, kind=None):
        rows = self.conn.execute('SELECT * FROM outbox ORDER BY id').fetchall()
        return [json.loads(row['payload']) for row in rows if kind is None or row['kind'] == kind]

    def drain(self, now=None):
        for _ in range(4):
            bot.deliver_outbox(self.vk, self.conn, self.now if now is None else now)

    def test_lexicon_uses_whole_words_and_phrases(self):
        for word in ('компания', 'температура', 'олимпиада'):
            self.assertFalse(bot.contains_any_prohibited_term(word), word)
        for text in ('МП!', 'без   предоплаты', 'цена: 3 тыс'):
            self.assertTrue(bot.contains_any_prohibited_term(text), text)

    def test_numeric_mentions_have_boundaries(self):
        self.assertEqual(bot.parse_target_user_id('/мут @123abc'), 0)
        self.assertEqual(bot.parse_target_user_id('/мут @id123abc'), 0)
        self.assertEqual(bot.parse_target_user_id('/мут @id123'), 123)
        self.assertEqual(bot.parse_target_user_id('/мут [id123|Иван]'), 123)

    def test_profile_alias_and_admin_commands(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/profile @kenaya', uid=99))
        self.assertIn('[id10|', self.queued('send')[0]['message'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM daily_user_stats').fetchone()[0], 0)

    def test_reputation_without_sign_and_duplicate_silence(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/rep @kenaya'))
        self.assertEqual(db.get_reputation(self.conn, 10), 1)
        bot.handle_new_message(self.vk, self.conn, self.message('/rep - @kenaya', mid=2))
        self.assertEqual(db.get_reputation(self.conn, 10), 1)
        self.assertEqual(len(self.queued('send')), 1)

    def test_commands_cannot_bypass_moderation(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/profile @all +79991234567'))
        self.assertEqual(len(self.queued('delete')), 1)
        self.assertEqual(len(self.queued('mute')), 1)
        self.assertEqual(db.get_command_cooldown(self.conn, 1, 'profile'), 0)

    def test_violations_commands_service_events_not_in_activity(self):
        events = [self.message('/stat', mid=1), self.message('+79991234567', mid=2),
                  self.message('', mid=3, action={'type': 'chat_invite_user'}),
                  self.message('/unknown', mid=4)]
        for event in events:
            bot.handle_new_message(self.vk, self.conn, event)
        self.assertEqual(db.get_daily_stats(self.conn, datetime.fromtimestamp(self.now, config.CHAT_TZ).date())['messages'], 0)

    def test_message_stats_and_cmid_deduplication(self):
        event = self.message(mid=0, conversation_message_id=7, attachments=[{'type': 'photo'}])
        bot.handle_new_message(self.vk, self.conn, event)
        bot.handle_new_message(self.vk, self.conn, event)
        stats = db.get_daily_stats(self.conn, datetime.fromtimestamp(self.now, config.CHAT_TZ).date())
        self.assertEqual((stats['messages'], stats['photos']), (1, 1))

    def test_failed_inbox_transaction_rolls_back_all_effects_and_retries(self):
        original = bot.process_message
        def fail(vk, conn, message):
            original(vk, conn, message)
            raise RuntimeError('failure after all local effects')
        with patch.object(bot, 'process_message', side_effect=fail):
            bot.handle_new_message(self.vk, self.conn, self.message('/rep @kenaya'))
        self.assertEqual(db.get_reputation(self.conn, 10), 0)
        self.assertEqual(len(self.queued()), 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM processed_messages').fetchone()[0], 0)
        self.conn.execute('UPDATE inbox SET next_attempt = 0')
        self.conn.commit()
        bot.process_inbox(self.vk, self.conn)
        self.assertEqual(db.get_reputation(self.conn, 10), 1)
        self.assertEqual(self.conn.execute('SELECT done_at FROM inbox').fetchone()[0], self.now)

    def test_send_retry_uses_stable_random_id_and_no_repeated_vote(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/rep @kenaya'))
        self.vk.messages.send.side_effect = [OSError('timeout'), 100]
        bot.deliver_outbox(self.vk, self.conn, self.now)
        bot.deliver_outbox(self.vk, self.conn, self.now + 5)
        calls = self.vk.messages.send.call_args_list
        self.assertEqual(calls[0].kwargs['random_id'], calls[1].kwargs['random_id'])
        self.assertNotEqual(calls[0].kwargs['random_id'], 0)
        self.assertEqual(db.get_reputation(self.conn, 10), 1)

    def test_mute_is_confirmed_only_after_vk_success_and_never_extended(self):
        peer = config.MODERATION_CHAT_PEER_ID
        bot.handle_new_message(self.vk, self.conn, self.message(peer=peer))
        bot.handle_new_message(self.vk, self.conn, self.message(mid=2, peer=peer))
        self.assertEqual(self.conn.execute('SELECT mute_until FROM users').fetchone()[0], 0)
        self.vk.messages.changeConversationMemberRestrictions.side_effect = [OSError('timeout'), 1]
        bot.deliver_outbox(self.vk, self.conn, self.now)
        self.assertEqual(self.conn.execute('SELECT mute_until FROM users').fetchone()[0], 0)
        bot.handle_new_message(self.vk, self.conn, self.message(mid=3, peer=peer))
        self.assertEqual(len(self.queued('mute')), 1)
        bot.deliver_outbox(self.vk, self.conn, self.now + 5)
        self.assertEqual(self.conn.execute('SELECT mute_until FROM users').fetchone()[0], self.now + 3600)
        self.assertEqual(self.vk.messages.changeConversationMemberRestrictions.call_args.kwargs['for'], 3595)
        self.assertEqual(self.conn.execute('SELECT SUM(mutes) FROM chat_mute_stats').fetchone()[0], 1)

    def test_failed_delete_delays_reason_but_not_mute(self):
        bot.handle_new_message(self.vk, self.conn, self.message('мп', peer=config.MODERATION_CHAT_PEER_ID))
        self.vk.messages.delete.side_effect = OSError('timeout')
        self.drain()
        self.assertEqual(self.vk.messages.changeConversationMemberRestrictions.call_count, 1)
        self.assertEqual(self.vk.messages.send.call_count, 0)
        self.vk.messages.delete.side_effect = None
        self.drain(self.now + 5)
        self.assertEqual(self.vk.messages.send.call_count, 1)
        self.assertIn('Test User,', self.vk.messages.send.call_args.kwargs['message'])
        self.assertEqual(self.conn.execute('SELECT delete_at FROM bot_messages').fetchone()[0], self.now + 65)

    def test_restriction_expiry_does_not_lift_an_admin_mute(self):
        db.confirm_mute(self.conn, config.MODERATION_CHAT_PEER_ID, 1, self.now - 1)
        bot.handle_new_message(self.vk, self.conn, self.message(peer=config.MODERATION_CHAT_PEER_ID))
        self.vk.messages.changeConversationMemberRestrictions.assert_not_called()

    def test_chat_mute_statistics_are_separate_and_legacy_preserved(self):
        today = datetime.fromtimestamp(self.now, config.CHAT_TZ).date()
        db.record_mute(self.conn, 1, self.now)  # Preserve old, ambiguous data.
        db.record_mute(self.conn, 1, self.now, peer_id=config.MODERATION_CHAT_PEER_ID)
        db.record_mute(self.conn, 1, self.now, peer_id=config.CHAT_PEER_ID)
        self.assertEqual(db.get_weekly_stats(self.conn, today, today + timedelta(days=1))['mutes'], 1)
        self.assertEqual(self.conn.execute('SELECT mutes FROM daily_stats').fetchone()[0], 1)

    def test_stat_failure_stays_pending_then_access_is_cached(self):
        self.vk.users.get.side_effect = OSError('timeout')
        bot.handle_new_message(self.vk, self.conn, self.message('/stat', uid=10))
        self.assertEqual(self.conn.execute('SELECT done_at FROM inbox').fetchone()[0], 0)
        self.vk.users.get.side_effect = self.profiles
        self.conn.execute('UPDATE inbox SET next_attempt = 0')
        self.conn.commit()
        bot.process_inbox(self.vk, self.conn)
        self.assertEqual(len(self.queued('send')), 1)
        bot.handle_new_message(self.vk, self.conn, self.message('/stat', uid=20, mid=2))
        self.assertEqual(len(self.queued('send')), 2)
        self.assertEqual(self.vk.users.get.call_count, 2)

    def test_stat_numeric_ids_no_lookup_and_unapproved_user_silent(self):
        with patch.object(config, 'STAT_ALLOWED_IDS', {10, 20, 1122341522}):
            bot.handle_new_message(self.vk, self.conn, self.message('/stat', uid=10))
            bot.handle_new_message(self.vk, self.conn, self.message('/stat', uid=30, mid=2))
        self.assertEqual(len(self.queued('send')), 1)
        self.vk.users.get.assert_not_called()

    def test_king_one_pending_and_one_confirmed_mute_per_day(self):
        today = datetime.fromtimestamp(self.now, config.CHAT_TZ).date()
        db.save_king(self.conn, today - timedelta(days=1), 1, 20)
        bot.handle_new_message(self.vk, self.conn, self.message('/мут @id2'))
        bot.handle_new_message(self.vk, self.conn, self.message('/мут @id3', mid=2))
        self.assertEqual(len(self.queued('mute')), 1)
        self.assertEqual(db.get_command_cooldown(self.conn, 1, 'king_mute'), 0)
        self.drain()
        self.assertEqual(db.get_command_cooldown(self.conn, 1, 'king_mute'), self.now)
        bot.handle_new_message(self.vk, self.conn, self.message('/мут @id4', mid=3))
        self.assertEqual(len(self.queued('mute')), 1)

    def test_expired_mute_job_does_not_apply_or_spend_king_right(self):
        bot.apply_mute(self.vk, config.CHAT_PEER_ID, 2, king_id=1, now=self.now)
        self.drain(self.now + 3601)
        self.vk.messages.changeConversationMemberRestrictions.assert_not_called()
        self.assertEqual(db.get_command_cooldown(self.conn, 1, 'king_mute'), 0)

    def test_restart_keeps_pending_delivery_and_deduplication(self):
        event = self.message('/rep @kenaya')
        bot.handle_new_message(self.vk, self.conn, event)
        self.conn.close()
        self.conn = db.connect(self.path)
        bot.ACTIVE_CONN = self.conn
        bot.handle_new_message(self.vk, self.conn, event)
        self.drain()
        self.assertEqual(self.vk.messages.send.call_count, 1)
        self.assertEqual(db.get_reputation(self.conn, 10), 1)

    def test_pruning_preserves_pending_jobs_and_user_history(self):
        ancient = self.now - config.HISTORY_RETENTION_SECONDS - 100
        db.add_reputation_vote(self.conn, 1, 2, 1, '2025-01-01', ancient)
        db.enqueue(self.conn, 'send', {'message': 'pending'})
        self.conn.execute('UPDATE outbox SET created_at = ?', (ancient,))
        self.conn.commit()
        db.claim_processed_message(self.conn, config.CHAT_PEER_ID, 9, ancient)
        db.prune_operational_history(self.conn, self.now)
        self.assertEqual(db.get_reputation(self.conn, 2), 1)
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM processed_messages').fetchone()[0], 0)

    def test_scheduler_recovers_missed_hour_and_deduplicates(self):
        today = datetime.fromtimestamp(self.now, config.CHAT_TZ).date()
        before = datetime.combine(today, datetime.min.time(), config.CHAT_TZ).replace(hour=11)
        after = before.replace(hour=14)
        bot.scheduler_tick(self.vk, self.conn, before)
        count = len(self.queued('send'))
        bot.scheduler_tick(self.vk, self.conn, after)
        self.assertEqual(len(self.queued('send')), count + 1)
        bot.scheduler_tick(self.vk, self.conn, after)
        self.assertEqual(len(self.queued('send')), count + 1)

    def test_scheduler_catches_midnight_king_and_weekly_report(self):
        sunday = datetime(2026, 10, 11, 23, 30, tzinfo=config.CHAT_TZ)
        db.record_message(self.conn, 42, int(sunday.timestamp()))
        # Establish checkpoint without generating first-start catchup output.
        db.claim_scheduler_event(self.conn, 'checkpoint', int(sunday.timestamp()))
        monday = sunday + timedelta(hours=14)
        bot.scheduler_tick(self.vk, self.conn, monday)
        self.assertEqual(db.get_king(self.conn, sunday.date()), (42, 1))
        reports = [p['message'] for p in self.queued('send')]
        self.assertTrue(any('ГЕРОЙ ЧАТА' in text for text in reports))
        self.assertTrue(any('ИТОГИ НЕДЕЛИ' in text for text in reports))
        count = len(reports)
        bot.scheduler_tick(self.vk, self.conn, monday)
        self.assertEqual(len(self.queued('send')), count)

    def test_scheduler_rollback_retries_without_duplicate_king(self):
        yesterday = datetime.fromtimestamp(self.now, config.CHAT_TZ).date() - timedelta(days=1)
        db.record_message(self.conn, 42, int(datetime.combine(yesterday, datetime.min.time(), config.CHAT_TZ).timestamp()))
        with patch.object(bot, 'publish_daily_stats', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                bot.scheduler_tick(self.vk, self.conn, datetime.fromtimestamp(self.now, config.CHAT_TZ))
        self.assertIsNone(db.get_king(self.conn, yesterday))
        self.assertEqual(len(self.queued()), 0)
        bot.scheduler_tick(self.vk, self.conn, datetime.fromtimestamp(self.now, config.CHAT_TZ))
        self.assertEqual(db.get_king(self.conn, yesterday), (42, 1))
        self.assertEqual(sum('ГЕРОЙ ЧАТА' in p['message'] for p in self.queued('send')), 1)

    def test_cleanup_survives_vk_failure_and_restart(self):
        db.queue_bot_message(self.conn, 777, config.MODERATION_CHAT_PEER_ID, self.now)
        bot.cleanup_tick(self.conn, self.now)
        bot.cleanup_tick(self.conn, self.now)
        self.assertEqual(len(self.queued('delete')), 1)
        self.vk.messages.delete.side_effect = OSError('timeout')
        bot.deliver_outbox(self.vk, self.conn, self.now)
        self.conn.close()
        self.conn = db.connect(self.path)
        bot.ACTIVE_CONN = self.conn
        self.vk.messages.delete.side_effect = None
        bot.deliver_outbox(self.vk, self.conn, self.now + 5)
        self.assertEqual(self.conn.execute('SELECT done_at FROM outbox').fetchone()[0], self.now + 5)

    def test_migration_failure_is_atomic_and_retryable(self):
        self.conn.execute('DELETE FROM schema_migrations WHERE version = 5')
        self.conn.commit()
        original = db._migration_5
        def fail(conn):
            original(conn)
            conn.execute('CREATE TABLE temporary_migration_probe(id INTEGER)')
            raise RuntimeError('migration failed')
        with patch.object(db, '_migration_5', side_effect=fail):
            with self.assertRaises(RuntimeError):
                db.connect(self.path)
        self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name = 'temporary_migration_probe'").fetchone())
        retry = db.connect(self.path)
        self.assertEqual(retry.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 5)
        retry.close()

    def test_confirmed_mute_survives_crash_before_db_commit(self):
        bot.apply_mute(self.vk, config.MODERATION_CHAT_PEER_ID, 2, reason='reason')
        with patch.object(db, 'confirm_mute', side_effect=RuntimeError('DB failure after VK')):
            bot.deliver_outbox(self.vk, self.conn, self.now)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM chat_mute_stats').fetchone()[0], 0)
        bot.deliver_outbox(self.vk, self.conn, self.now + 5)
        self.assertEqual(self.vk.messages.changeConversationMemberRestrictions.call_args.kwargs['for'], 3595)
        self.assertEqual(self.conn.execute('SELECT SUM(mutes) FROM chat_mute_stats').fetchone()[0], 1)
        self.assertEqual(len(self.queued('send')), 1)

    def test_backup_api_preserves_wal_rows(self):
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('INSERT INTO users(user_id, peer_id, warnings) VALUES (77, 2000000001, 5)')
        self.conn.execute('DELETE FROM schema_migrations WHERE version = 5')
        self.conn.commit()
        migrated = db.connect(self.path)
        migrated.close()
        backup = next((Path(self.tmp.name) / 'backups').glob('*.db'))
        import sqlite3
        with sqlite3.connect(backup) as reader:
            self.assertEqual(reader.execute('SELECT warnings FROM users WHERE user_id = 77').fetchone()[0], 5)

    def test_startup_constructs_supported_vk_session_and_workers(self):
        class FakeLongPoll:
            def __init__(self, session, group_id):
                self.session = session
            def listen(self):
                raise KeyboardInterrupt
        handle = MagicMock()
        with patch.object(bot, 'acquire_instance_lock', return_value=handle), \
             patch.object(db, 'connect', return_value=self.conn), \
             patch.object(bot.threading, 'Thread') as thread, \
             patch.object(bot, 'VkBotLongPoll', FakeLongPoll):
            with self.assertRaises(KeyboardInterrupt):
                bot.run_forever()
            self.assertEqual(thread.call_count, 3)

    def test_second_local_instance_is_rejected(self):
        with patch.object(config, 'BASE_DIR', Path(self.tmp.name)):
            handle = bot.acquire_instance_lock()
            try:
                with self.assertRaises(SystemExit):
                    bot.acquire_instance_lock()
            finally:
                handle.close()
