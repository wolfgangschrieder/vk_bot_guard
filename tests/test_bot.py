import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
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
        self.vk.messages.delete.return_value = 1
        self.vk.messages.changeConversationMemberRestrictions.return_value = 1
        self.vk.messages.removeChatUser.return_value = 1
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

    def test_admin_can_add_and_remove_social_chat_blacklist_entry(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/чс @kenaya', uid=99))
        self.assertTrue(db.is_chat_blacklisted(self.conn, 10))
        self.vk.messages.removeChatUser.assert_called_once_with(chat_id=2, user_id=10)
        self.assertIn('добавлен в ЧС', self.queued('send')[0]['message'])

        bot.handle_new_message(self.vk, self.conn, self.message('/чс снять @kenaya', uid=99, mid=2))
        self.assertFalse(db.is_chat_blacklisted(self.conn, 10))
        self.assertIn('удалён из чёрного списка', self.queued('send')[-1]['message'])

    def test_non_admin_cannot_change_blacklist(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/чс @kenaya', uid=1))
        self.assertFalse(db.is_chat_blacklisted(self.conn, 10))
        self.vk.messages.removeChatUser.assert_not_called()
        self.assertIn('только администраторам', self.queued('send')[0]['message'])

    def test_four_repeated_words_mute_only_in_social_chat(self):
        bot.handle_new_message(
            self.vk, self.conn,
            self.message('да да, да да', peer=config.CHAT_PEER_ID),
        )
        mute = self.queued('mute')[0]
        self.assertEqual(mute['peer_id'], config.CHAT_PEER_ID)
        self.assertEqual(mute['until'] - self.now, 3600)
        self.assertEqual(mute['reason'], 'Спам запрещен!')
        self.assertEqual(len(self.queued('delete')), 1)

        self.vk.messages.delete.reset_mock()
        bot.handle_new_message(
            self.vk, self.conn,
            self.message('да да да да', peer=config.MODERATION_CHAT_PEER_ID, mid=2),
        )
        self.assertEqual(len(self.queued('mute')), 1)

    def test_four_identical_consecutive_stickers_mute_and_queue_reason(self):
        for mid in range(1, 5):
            bot.handle_new_message(self.vk, self.conn, self.message(
                '', mid=mid,
                attachments=[{'type': 'sticker', 'sticker': {'sticker_id': 77, 'product_id': 1}}],
            ))
        mute = self.queued('mute')[0]
        self.assertEqual(mute['reason'], 'Спам запрещен!')
        self.assertEqual(mute['until'] - self.now, 3600)
        self.assertEqual(len(self.queued('delete')), 1)
        self.drain()
        self.assertTrue(any(
            item['message'] == 'Test User, Спам запрещен!'
            for item in self.queued('send')
        ))

    def test_non_sticker_breaks_repeated_sticker_streak(self):
        sticker = [{'type': 'sticker', 'sticker': {'sticker_id': 77}}]
        for mid in (1, 2):
            bot.handle_new_message(self.vk, self.conn, self.message('', mid=mid, attachments=sticker))
        bot.handle_new_message(self.vk, self.conn, self.message('обычное сообщение', mid=3))
        for mid in (4, 5):
            bot.handle_new_message(self.vk, self.conn, self.message('', mid=mid, attachments=sticker))
        self.assertEqual(self.queued('mute'), [])

    def test_blacklist_is_kept_when_vk_does_not_confirm_removal(self):
        self.vk.messages.removeChatUser.return_value = 0
        bot.handle_new_message(self.vk, self.conn, self.message('/чс @kenaya', uid=99))
        self.assertTrue(db.is_chat_blacklisted(self.conn, 10))
        self.assertIn('VK пока не подтвердил удаление', self.queued('send')[0]['message'])

    def test_blacklisted_user_is_removed_on_message_and_reinvite(self):
        db.add_chat_blacklist(self.conn, 10, 99, self.now)
        bot.handle_new_message(self.vk, self.conn, self.message('hello', uid=10))
        self.vk.messages.removeChatUser.assert_called_once_with(chat_id=2, user_id=10)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM daily_user_stats').fetchone()[0], 0)

        event = SimpleNamespace(obj={
            'peer_id': config.CHAT_PEER_ID,
            'action': {'type': 'chat_invite_user', 'member_id': 10},
        })
        bot.handle_chat_update(self.vk, event, self.conn)
        self.assertEqual(self.vk.messages.removeChatUser.call_count, 2)

    def test_startup_scan_removes_blacklisted_members(self):
        db.add_chat_blacklist(self.conn, 10, 99, self.now)
        self.vk.messages.getConversationMembers.return_value = {
            'items': [{'member_id': 10}, {'member_id': 11}],
        }
        bot.scan_chat_blacklist(self.vk, self.conn)
        self.vk.messages.getConversationMembers.assert_called_once_with(
            peer_id=config.CHAT_PEER_ID, count=1000, offset=0,
        )
        self.vk.messages.removeChatUser.assert_called_once_with(chat_id=2, user_id=10)

    def test_public_menu_has_profile_and_user_guide_buttons(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/menu', uid=1))
        payload = self.queued('send')[0]
        self.assertIn('/profile', payload['message'])
        self.assertTrue(payload['temporary'])
        self.assertEqual(payload['delay'], 120)
        self.assertEqual(payload['keyboard']['buttons'][0][0]['action'], {
            'type': 'text', 'label': '👤 Мой профиль',
        })
        self.assertEqual(payload['keyboard']['buttons'][0][1]['action'], {
            'type': 'open_link',
            'link': 'https://vk.ru/topic-234090559_68552562?offset=0',
            'label': '📖 Руководство',
        })
        bot.deliver_outbox(self.vk, self.conn, self.now)
        keyboard = json.loads(self.vk.messages.send.call_args.kwargs['keyboard'])
        self.assertTrue(keyboard['inline'])
        self.assertEqual(keyboard, payload['keyboard'])
        bot.handle_new_message(self.vk, self.conn, self.message('👤 Мой профиль', mid=2))
        self.assertIn('ПРОФИЛЬ', self.queued('send')[-1]['message'])

    def test_menu_is_limited_to_one_call_per_local_day(self):
        for message_id in range(1, 3):
            bot.handle_new_message(self.vk, self.conn, self.message('/menu', mid=message_id))
        sends = self.queued('send')
        self.assertEqual(len(sends), 2)  # One menu and one limit notice.
        self.assertEqual(sum(bool(item['keyboard']) for item in sends), 1)
        self.assertTrue(all(item['temporary'] and item['delay'] == 120 for item in sends))

        bot.handle_new_message(self.vk, self.conn, self.message('/menu', mid=3))
        self.assertEqual(len(self.queued('send')), 2)  # No repeated limit notices.

        next_day_now = self.now + 24 * 60 * 60
        with patch('bot.time.time', return_value=next_day_now):
            bot.handle_new_message(self.vk, self.conn, self.message('/menu', mid=4))
        sends = self.queued('send')
        self.assertEqual(sum(bool(item['keyboard']) for item in sends), 2)

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

    def test_conversation_deletion_uses_cmid_and_peer_id(self):
        event = self.message(
            text='мп',
            peer=config.MODERATION_CHAT_PEER_ID,
            conversation_message_id=77,
        )
        bot.delete_message(self.vk, event)
        params = self.queued('delete')[0]['params']
        self.assertEqual(params, {
            'peer_id': config.MODERATION_CHAT_PEER_ID,
            'cmids': '77',
            'delete_for_all': 1,
        })

    def test_global_id_deletion_still_includes_peer_id(self):
        event = self.message(
            text='мп',
            peer=config.MODERATION_CHAT_PEER_ID,
            conversation_message_id=0,
            mid=123,
        )
        bot.delete_message(self.vk, event)
        params = self.queued('delete')[0]['params']
        self.assertEqual(params, {
            'peer_id': config.MODERATION_CHAT_PEER_ID,
            'message_ids': '123',
            'delete_for_all': 1,
        })

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

    def test_daily_report_publishes_active_users_and_activity_percent(self):
        stat_date = datetime.fromtimestamp(self.now, config.CHAT_TZ).date()
        stats = {
            'date': stat_date.isoformat(), 'messages': 120, 'active_users': 18,
            'activity_percent': 150, 'activity_average_messages': 80,
            'photos': 0, 'videos': 0, 'music': 0, 'voices': 0, 'top_users': [],
        }
        with patch.object(db, 'get_daily_stats', return_value=stats):
            self.assertTrue(bot.publish_daily_stats(self.vk, self.conn, stat_date))

        report = self.queued('send')[0]['message']
        self.assertIn('👥 Активных участников: 18', report)
        self.assertIn('⚡ Активность чата: 150% от среднего за 7 дней', report)

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
        self.conn.execute('DELETE FROM schema_migrations WHERE version = 8')
        self.conn.commit()
        original = db._migration_8
        def fail(conn):
            original(conn)
            conn.execute('CREATE TABLE temporary_migration_probe(id INTEGER)')
            raise RuntimeError('migration failed')
        with patch.object(db, '_migration_8', side_effect=fail):
            with self.assertRaises(RuntimeError):
                db.connect(self.path)
        self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name = 'temporary_migration_probe'").fetchone())
        retry = db.connect(self.path)
        self.assertEqual(retry.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], db.SCHEMA_VERSION)
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
        self.conn.execute('DELETE FROM schema_migrations WHERE version = 8')
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
            self.assertEqual(thread.call_count, 4)

    def operation(self, kind, payload):
        operation_id = db.enqueue(self.conn, kind, payload)
        return self.conn.execute('SELECT * FROM outbox WHERE id = ?', (operation_id,)).fetchone()

    def test_structured_send_with_zero_global_id_cleans_up_by_cmid(self):
        bot.handle_new_message(self.vk, self.conn, self.message('мп', peer=config.MODERATION_CHAT_PEER_ID))
        self.vk.messages.send.return_value = [{'peer_id': config.MODERATION_CHAT_PEER_ID,
            'message_id': 0, 'conversation_message_id': 77}]
        self.drain()
        task = self.conn.execute('SELECT * FROM cleanup_tasks').fetchone()
        self.assertEqual((task['peer_id'], task['cmid']), (config.MODERATION_CHAT_PEER_ID, 77))
        self.assertEqual(self.vk.messages.send.call_args.kwargs['peer_ids'], str(config.MODERATION_CHAT_PEER_ID))
        self.assertEqual(self.vk.messages.send.call_count, 1)
        bot.cleanup_tick(self.conn, self.now + 60)
        self.vk.messages.delete.return_value = [{'peer_id': config.MODERATION_CHAT_PEER_ID,
            'conversation_message_id': 77, 'message_id': 0, 'response': 1}]
        bot.deliver_outbox(self.vk, self.conn, self.now + 60)
        self.assertEqual(self.vk.messages.delete.call_args.kwargs['cmids'], '77')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0], 0)

    def test_zero_send_is_quarantined_without_resend(self):
        self.operation('send', {'peer_id': config.CHAT_PEER_ID, 'message': 'test', 'temporary': False, 'delay': 60})
        self.vk.messages.send.return_value = 0
        self.drain()
        row = self.conn.execute("SELECT * FROM outbox WHERE kind = 'send'").fetchone()
        self.assertEqual(row['status'], 'failed')
        self.assertIn('Неоднозначный', row['last_error'])
        self.assertEqual(self.vk.messages.send.call_count, 1)

    def test_send_nested_access_error_is_terminal(self):
        self.operation('send', {'peer_id': config.CHAT_PEER_ID, 'message': 'test', 'temporary': False, 'delay': 60})
        self.vk.messages.send.return_value = [{'peer_id': config.CHAT_PEER_ID, 'message_id': 0,
            'conversation_message_id': 0, 'error': {'code': 917, 'message': 'no access'}}]
        self.drain()
        row = self.conn.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual(row['status'], 'failed')
        self.assertIn('917', row['last_error'])
        self.assertEqual(self.vk.messages.send.call_count, 1)

    def test_failed_delete_blocks_reason(self):
        bot.handle_new_message(self.vk, self.conn, self.message('мп', peer=config.MODERATION_CHAT_PEER_ID))
        self.vk.messages.delete.return_value = [{'peer_id': config.MODERATION_CHAT_PEER_ID,
            'message_id': 1, 'conversation_message_id': 7, 'response': 0,
            'error': {'code': 924, 'message': 'cannot delete'}}]
        self.drain()
        self.vk.messages.send.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT status FROM outbox WHERE kind = 'delete'").fetchone()[0], 'failed')
        self.assertEqual(self.conn.execute("SELECT status FROM outbox WHERE kind = 'send'").fetchone()[0], 'failed')

    def test_partial_delete_does_not_confirm_whole_request(self):
        row = self.operation('delete', {'params': {'message_ids': '1,2', 'delete_for_all': 1}})
        self.vk.messages.delete.return_value = [{'message_id': 1, 'response': 1}]
        with self.assertRaises(bot.DeliveryError):
            bot.execute_operation(self.vk, self.conn, row, self.now)
        self.assertEqual(self.conn.execute('SELECT done_at FROM outbox').fetchone()[0], 0)

    def test_legacy_cmid_parameter_is_normalized(self):
        row = self.operation('delete', {'params': {'peer_id': config.CHAT_PEER_ID,
            'conversation_message_ids': '77', 'delete_for_all': 1}})
        self.vk.messages.delete.return_value = [{'peer_id': config.CHAT_PEER_ID,
            'conversation_message_id': 77, 'response': 1}]
        bot.execute_operation(self.vk, self.conn, row, self.now)
        self.assertEqual(self.vk.messages.delete.call_args.kwargs['cmids'], '77')
        self.assertNotIn('conversation_message_ids', self.vk.messages.delete.call_args.kwargs)

    def test_missing_dependency_fails_without_notification(self):
        self.operation('send', {'peer_id': config.CHAT_PEER_ID, 'message': 'test',
            'temporary': False, 'delay': 60, 'depends_on': 9999})
        self.drain()
        self.vk.messages.send.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT status FROM outbox').fetchone()[0], 'failed')

    def test_wrong_chat_jobs_are_cancelled_not_retargeted(self):
        self.operation('send', {'peer_id': 2000000387, 'message': 'old', 'temporary': False, 'delay': 60})
        self.drain()
        self.vk.messages.send.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT status FROM outbox').fetchone()[0], 'cancelled')

    def test_existing_hundreds_of_retries_are_stopped(self):
        row = self.operation('send', {'peer_id': config.CHAT_PEER_ID, 'message': 'old', 'temporary': False, 'delay': 60})
        self.conn.execute('UPDATE outbox SET attempts = 333 WHERE id = ?', (row['id'],))
        self.conn.commit()
        self.drain()
        self.vk.messages.send.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT status FROM outbox').fetchone()[0], 'failed')

    def test_retry_budget_is_bounded(self):
        self.operation('send', {'peer_id': config.CHAT_PEER_ID, 'message': 'test', 'temporary': False, 'delay': 60})
        self.vk.messages.send.side_effect = OSError('timeout')
        for attempt in range(config.DELIVERY_MAX_ATTEMPTS + 3):
            bot.deliver_outbox(self.vk, self.conn, self.now + attempt * 300)
        row = self.conn.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['attempts'], config.DELIVERY_MAX_ATTEMPTS)
        self.assertEqual(self.vk.messages.send.call_count, config.DELIVERY_MAX_ATTEMPTS)

    def test_stale_notifications_are_not_sent(self):
        self.operation('send', {'peer_id': config.CHAT_PEER_ID, 'message': 'old', 'temporary': False, 'delay': 60})
        self.drain(self.now + config.SEND_MAX_AGE_SECONDS + 1)
        self.vk.messages.send.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT status FROM outbox').fetchone()[0], 'cancelled')

    def test_failed_king_request_releases_pending_right(self):
        today = datetime.fromtimestamp(self.now, config.CHAT_TZ).date()
        db.save_king(self.conn, today - timedelta(days=1), 1, 20)
        bot.handle_new_message(self.vk, self.conn, self.message('/мут @id2'))
        self.vk.messages.changeConversationMemberRestrictions.return_value = {'error': {'code': 935, 'message': 'not member'}}
        self.drain()
        self.assertFalse(bot.pending_king_mute(self.conn, 1, today))
        self.assertEqual(db.get_command_cooldown(self.conn, 1, 'king_mute'), 0)
        self.vk.messages.changeConversationMemberRestrictions.return_value = 1
        bot.handle_new_message(self.vk, self.conn, self.message('/мут @id3', mid=2))
        self.drain()
        self.assertEqual(db.get_command_cooldown(self.conn, 1, 'king_mute'), self.now)
        self.assertEqual(self.conn.execute('SELECT SUM(mutes) FROM chat_mute_stats').fetchone()[0], 1)

    def test_forwarded_content_cannot_bypass_rules(self):
        nested = {'text': '', 'fwd_messages': [{'text': '', 'attachments': [{'type': 'video'}]}]}
        bot.handle_new_message(self.vk, self.conn, self.message(peer=config.MODERATION_CHAT_PEER_ID, fwd_messages=[nested]))
        self.assertEqual(len(self.queued('delete')), 1)
        self.assertEqual(len(self.queued('mute')), 1)

    def test_quoted_reply_does_not_punish_author(self):
        bot.handle_new_message(self.vk, self.conn, self.message(peer=config.MODERATION_CHAT_PEER_ID,
            reply_message={'text': 'мп'}))
        self.assertEqual(len(self.queued('mute')), 0)

    def test_cleanup_survives_reopen_with_conversation_ids(self):
        db.queue_cleanup(self.conn, config.CHAT_PEER_ID, 0, 9, self.now)
        self.conn.close()
        self.conn = db.connect(self.path)
        bot.ACTIVE_CONN = self.conn
        bot.cleanup_tick(self.conn, self.now)
        self.vk.messages.delete.return_value = [{'peer_id': config.CHAT_PEER_ID, 'conversation_message_id': 9, 'response': 1}]
        self.drain()
        self.assertEqual(self.conn.execute('SELECT status FROM outbox').fetchone()[0], 'done')

    def test_second_local_instance_is_rejected(self):
        with patch.object(config, 'BASE_DIR', Path(self.tmp.name)):
            handle = bot.acquire_instance_lock()
            try:
                with self.assertRaises(SystemExit):
                    bot.acquire_instance_lock()
            finally:
                handle.close()
