import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
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
        self.vk.messages.getConversationMembers.return_value = {
            'items': [{'member_id': user_id} for user_id in range(1, 101)],
        }
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

    def grant_donut(self, user_id, event_id=None):
        event = {
            'event_id': event_id or f'test-donut-{user_id}',
            'type': 'donut_subscription_create',
            'object': {'user_id': user_id, 'amount': config.PREMIUM_DONUT_MIN_AMOUNT_RUB},
        }
        return db.process_donut_event(
            self.conn, event, config.PREMIUM_DONUT_MIN_AMOUNT_RUB, now=self.now,
        )

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
        profile = self.queued('send')[0]['message']
        self.assertEqual(profile.splitlines()[0], '👤 [id10|Test User]')
        self.assertNotIn('🛡️ АДМИНИСТРАТОР ЧАТА', profile)
        self.assertNotIn('ПРОФИЛЬ УЧАСТНИКА', profile)
        self.assertNotIn('Герой дня:', profile)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM daily_user_stats').fetchone()[0], 0)

    def test_admin_profile_has_distinct_shield_status_and_keeps_message_rank(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/profile @id99', uid=1))
        profile = self.queued('send')[0]['message']
        self.assertEqual(profile.splitlines()[0], '👤 [id99|Test User]')
        self.assertEqual(profile.splitlines()[2], '🛡️ АДМИНИСТРАТОР ЧАТА')
        self.assertIn('🎖 Ранг: Новичок', profile)
        self.assertIn('💬 Сообщений за всё время: 0', profile)

    def test_profile_shows_reputation_hero_activity_and_best_day(self):
        today = datetime.fromtimestamp(self.now, config.CHAT_TZ).date()
        daily_counts = {
            today: 34,
            today - timedelta(days=1): 61,
            today - timedelta(days=2): 20,
            today - timedelta(days=3): 20,
            today - timedelta(days=4): 15,
            today - timedelta(days=5): 12,
            today - timedelta(days=6): 20,
        }
        for stat_date, count in daily_counts.items():
            timestamp = int(datetime.combine(
                stat_date, datetime.min.time(), tzinfo=config.CHAT_TZ
            ).timestamp())
            for _ in range(count):
                db.record_message(self.conn, 10, timestamp)
        for offset in range(3):
            db.save_king(self.conn, today - timedelta(days=offset + 1), 10, 5)
        for index in range(12):
            self.conn.execute(
                'INSERT INTO reputation_votes(giver_id, receiver_id, week_key, value, created_at) '
                'VALUES (?, 10, ?, 1, ?)',
                (100 + index, f'week-{index}', self.now),
            )
        self.conn.commit()

        bot.handle_new_message(self.vk, self.conn, self.message('/profile @kenaya', uid=99))
        profile = self.queued('send')[0]['message']
        self.assertTrue(profile.startswith('👤 [id10|Test User]'))
        self.assertNotIn('ПРОФИЛЬ УЧАСТНИКА', profile)
        self.assertIn('⭐ Репутация: +12', profile)
        self.assertIn('🎖 Ранг: Новичок', profile)
        self.assertIn('💬 Сообщений за всё время: 182', profile)
        self.assertIn('🏆 Герой дня: 3 раза (последний: 09.10.2026)', profile)
        self.assertIn('Сегодня: 34 сообщения', profile)
        self.assertIn('За 7 дней: 182 сообщения', profile)
        self.assertIn('8 октября — 61 сообщение', profile)

    def test_marriage_proposal_requires_target_acceptance_and_appears_in_profile_and_pairs(self):
        self.grant_donut(1)
        self.grant_donut(2)
        bot.handle_new_message(self.vk, self.conn, self.message('/свадьба + @id2', uid=1))
        proposal = self.queued('send')[-1]['message']
        self.assertIn('предлагает', proposal)
        self.assertIn('/да @id1', proposal)
        self.assertIsNone(db.get_marriage(self.conn, 1))

        bot.handle_new_message(self.vk, self.conn, self.message('/да @id1', uid=3, mid=2))
        self.assertIsNone(db.get_marriage(self.conn, 1))

        bot.handle_new_message(self.vk, self.conn, self.message('/да @id1', uid=2, mid=3))
        self.assertEqual(db.get_marriage(self.conn, 1), (2, self.now))
        self.assertEqual(db.get_marriage(self.conn, 2), (1, self.now))
        self.assertIn('теперь пара', self.queued('send')[-1]['message'])

        bot.handle_new_message(self.vk, self.conn, self.message('/profile @id1', uid=99, mid=4))
        self.assertIn('💍 В паре с: [id2|Test User]', self.queued('send')[-1]['message'])
        self.assertIn('💒 Вместе с: 09.10.2026', self.queued('send')[-1]['message'])

        bot.handle_new_message(self.vk, self.conn, self.message('/пары', uid=3, mid=5))
        pair_list = self.queued('send')[-1]['message']
        self.assertIn('[id1|Test User] 💞 [id2|Test User]', pair_list)
        self.assertIn('Всего пар: 1', pair_list)

    def test_both_spouses_need_active_donut_when_proposing_and_accepting(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/свадьба @id2', uid=1))
        self.assertIsNone(db.get_marriage_proposal(self.conn, 1, 2, self.now + 1))

        self.grant_donut(1)
        bot.handle_new_message(self.vk, self.conn, self.message('/свадьба @id2', uid=1, mid=2))
        self.assertIsNone(db.get_marriage_proposal(self.conn, 1, 2, self.now + 1))

        self.grant_donut(2)
        bot.handle_new_message(self.vk, self.conn, self.message('/свадьба @id2', uid=1, mid=3))
        self.assertIsNotNone(db.get_marriage_proposal(self.conn, 1, 2, self.now + 1))

        expired = {
            'event_id': 'expire-during-proposal', 'type': 'donut_subscription_expired',
            'object': {'user_id': 2},
        }
        db.process_donut_event(self.conn, expired, config.PREMIUM_DONUT_MIN_AMOUNT_RUB,
                               now=self.now + 10)
        bot.handle_new_message(self.vk, self.conn, self.message('/да @id1', uid=2, mid=4))
        self.assertIsNone(db.get_marriage(self.conn, 1))

        self.grant_donut(2, 'renewed-donut-2')
        bot.handle_new_message(self.vk, self.conn, self.message('/да @id1', uid=2, mid=5))
        self.assertEqual(db.get_marriage(self.conn, 1), (2, self.now))

    def test_marriage_rejects_self_blacklisted_absent_and_already_engaged_users(self):
        for user_id in (1, 2, 3):
            self.grant_donut(user_id)
        bot.handle_new_message(self.vk, self.conn, self.message('/свадьба @id1', uid=1))
        self.assertIsNone(db.get_marriage_proposal(self.conn, 1, 1, self.now + 1))

        self.vk.messages.getConversationMembers.return_value = {'items': [{'member_id': 1}]}
        bot.handle_new_message(self.vk, self.conn, self.message('/свадьба @id2', uid=1, mid=2))
        self.assertIsNone(db.get_marriage_proposal(self.conn, 1, 2, self.now + 1))

        self.vk.messages.getConversationMembers.return_value = {
            'items': [{'member_id': user_id} for user_id in range(1, 101)],
        }
        db.add_chat_blacklist(self.conn, 2, 99, self.now)
        bot.handle_new_message(self.vk, self.conn, self.message('/свадьба @id2', uid=1, mid=3))
        self.assertIsNone(db.get_marriage_proposal(self.conn, 1, 2, self.now + 1))

        db.remove_chat_blacklist(self.conn, 2)
        bot.handle_new_message(self.vk, self.conn, self.message('/свадьба @id2', uid=1, mid=4))
        bot.handle_new_message(self.vk, self.conn, self.message('/свадьба @id3', uid=2, mid=5))
        self.assertIsNone(db.get_marriage_proposal(self.conn, 2, 3, self.now + 1))

    def test_proposals_expire_can_be_cancelled_and_one_user_cannot_stack_them(self):
        self.grant_donut(1)
        self.grant_donut(2)
        self.grant_donut(3)
        bot.handle_new_message(self.vk, self.conn, self.message('/свадьба @id2', uid=1))
        bot.handle_new_message(self.vk, self.conn, self.message('/свадьба @id3', uid=1, mid=2))
        self.assertIsNotNone(db.get_marriage_proposal(self.conn, 1, 2, self.now + 1))
        self.assertIsNone(db.get_marriage_proposal(self.conn, 1, 3, self.now + 1))

        bot.handle_new_message(self.vk, self.conn, self.message('/отменить свадьбу', uid=1, mid=3))
        self.assertIsNone(db.get_marriage_proposal(self.conn, 1, 2, self.now + 1))

        status = db.create_marriage_proposal(self.conn, 1, 2, self.now, config.MARRIAGE_PROPOSAL_SECONDS)
        self.assertEqual(status, 'created')
        expired = db.accept_marriage_proposal(
            self.conn, 1, 2, self.now + config.MARRIAGE_PROPOSAL_SECONDS,
        )
        self.assertEqual(expired, 'not_found')
        self.assertIsNone(db.get_marriage(self.conn, 1))

    def test_only_active_don_can_divorce_their_own_pair(self):
        db.create_marriage_proposal(self.conn, 1, 2, self.now, config.MARRIAGE_PROPOSAL_SECONDS)
        db.accept_marriage_proposal(self.conn, 1, 2, self.now + 1)
        bot.handle_new_message(self.vk, self.conn, self.message('/развод + @id2', uid=1))
        self.assertIsNotNone(db.get_marriage(self.conn, 1))

        event = {
            'event_id': 'wedding-don', 'type': 'donut_subscription_create',
            'object': {'user_id': 1, 'amount': 50},
        }
        self.assertEqual(db.process_donut_event(self.conn, event, 50, now=self.now), 'activated')
        bot.handle_new_message(self.vk, self.conn, self.message('/развод + @id3', uid=1, mid=2))
        self.assertIsNotNone(db.get_marriage(self.conn, 1))

        bot.handle_new_message(self.vk, self.conn, self.message('/развод + @id2', uid=1, mid=3))
        self.assertIsNone(db.get_marriage(self.conn, 1))
        self.assertIsNone(db.get_marriage(self.conn, 2))
        self.assertIn('Брак расторгнут', self.queued('send')[-1]['message'])

    def test_period_commands_publish_top_five_for_rolling_windows(self):
        today = datetime.fromtimestamp(self.now, config.CHAT_TZ).date()

        def add_messages(user_id, day, count):
            start = int(datetime.combine(day, datetime.min.time(), config.CHAT_TZ).timestamp())
            for offset in range(count):
                db.record_message(self.conn, user_id, start + 12 * 3600 + offset)

        add_messages(1, today, 5)
        add_messages(2, today - timedelta(days=2), 3)
        add_messages(3, today - timedelta(days=20), 7)
        add_messages(4, today - timedelta(days=200), 9)
        for index, command in enumerate(('/неделя', '/месяц', '/год'), start=1):
            bot.handle_new_message(self.vk, self.conn, self.message(command, uid=99, mid=index))
        reports = [item['message'] for item in self.queued('send')]
        self.assertIn('📊 ТОП-5 ЗА НЕДЕЛЯ', reports[0])
        self.assertIn('🥇 [id1|Test User] — 5', reports[0])
        self.assertIn('🥈 [id2|Test User] — 3', reports[0])
        self.assertIn('📊 ТОП-5 ЗА МЕСЯЦ', reports[1])
        self.assertIn('🥇 [id3|Test User] — 7', reports[1])
        self.assertIn('📊 ТОП-5 ЗА ГОД', reports[2])
        self.assertIn('🥇 [id4|Test User] — 9', reports[2])

    def test_period_top_commands_are_admin_only(self):
        for index, command in enumerate(('/неделя', '/месяц', '/год'), start=1):
            bot.handle_new_message(self.vk, self.conn, self.message(command, uid=1, mid=index))
        self.assertEqual(self.queued('send'), [])
        self.assertEqual(len(self.queued('delete')), 3)

    def test_commands_link_command_is_admin_only_and_has_short_button(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/команды', uid=99))
        payload = self.queued('send')[0]
        self.assertIn('Полный список команд', payload['message'])
        self.assertEqual(payload['keyboard']['buttons'][0][0]['action'], {
            'type': 'open_link', 'link': bot.COMMANDS_URL, 'label': '🤖 Команды бота',
        })

        bot.handle_new_message(self.vk, self.conn, self.message('/команды', uid=1, mid=2))
        self.assertEqual(len(self.queued('send')), 1)
        self.assertEqual(len(self.queued('delete')), 1)

    def test_morning_post_formats_date_weather_advice_and_question(self):
        report = bot.build_morning_message(
            date(2026, 10, 9),
            '🌦 Волгоград: +2…+8°C, слабый дождь, осадки: 45%, ветер до 18 км/ч. (Open-Meteo)',
            {'text': 'Возьми с собой зонт.'},
            {'text': 'Что сегодня может сделать день лучше?'},
        )
        self.assertIn('☀️ Доброе утро, чат!', report)
        self.assertIn('📅 Сегодня: пятница, 9 октября', report)
        self.assertIn('🌦 Волгоград:', report)
        self.assertIn('🧥 Совет дня: Возьми с собой зонт.', report)
        self.assertIn('💬 Вопрос дня: Что сегодня может сделать день лучше?', report)

    def test_morning_post_is_scheduled_once_at_eight_moscow_time(self):
        morning = datetime(2026, 10, 9, 8, 5, tzinfo=config.CHAT_TZ)
        content = {
            'advice': [{'id': 'a1', 'text': 'Проветри комнату.'}],
            'question': [{'id': 'q1', 'text': 'Как настроение?'}],
        }
        with patch.object(bot.morning_content, 'load_content', return_value=content) as load, \
                patch.object(bot.morning_content, 'fetch_weather', return_value='🌤 Волгоград: ясно') as weather:
            bot.scheduler_tick(self.vk, self.conn, morning)
            bot.scheduler_tick(self.vk, self.conn, morning + timedelta(minutes=1))
        reports = [item['message'] for item in self.queued('send')]
        morning_reports = [text for text in reports if text.startswith('☀️ Доброе утро, чат!')]
        self.assertEqual(len(morning_reports), 1)
        self.assertIn('🧥 Совет дня: Проветри комнату.', morning_reports[0])
        self.assertIn('💬 Вопрос дня: Как настроение?', morning_reports[0])
        load.assert_called_once_with(morning.date())
        weather.assert_called_once()

    def test_project_news_is_published_once_at_scheduled_time(self):
        publish_at = datetime(2026, 11, 14, 12, 30, tzinfo=config.CHAT_TZ)
        news = [{"datetime": publish_at.replace(tzinfo=None).isoformat(timespec="minutes"),
                 "text": "В чате появился новый раздел!"}]
        with patch.object(bot.morning_content, 'load_news', return_value=news):
            bot.scheduler_tick(self.vk, self.conn, publish_at + timedelta(minutes=2))
            bot.scheduler_tick(self.vk, self.conn, publish_at + timedelta(minutes=3))
        reports = [item['message'] for item in self.queued('send')]
        news_reports = [text for text in reports if text.startswith('📰 Новости проекта')]
        self.assertEqual(news_reports, ['📰 Новости проекта\n\nВ чате появился новый раздел!'])

    def test_project_news_outside_late_window_is_skipped(self):
        publish_at = datetime(2026, 11, 14, 12, 30, tzinfo=config.CHAT_TZ)
        news = [{"datetime": publish_at.replace(tzinfo=None).isoformat(timespec="minutes"),
                 "text": "Устаревшая новость"}]
        with patch.object(bot.morning_content, 'load_news', return_value=news):
            bot.scheduler_tick(self.vk, self.conn, publish_at + timedelta(minutes=31))
        self.assertFalse(any(item['message'].startswith('📰 Новости проекта')
                             for item in self.queued('send')))

    def test_legends_command_lists_everyone_at_threshold_in_descending_order(self):
        self.conn.executemany(
            "INSERT INTO daily_user_stats(stat_date, user_id, messages) VALUES (?, ?, ?)",
            [
                ('2026-10-01', 10, 100_000),
                ('2026-10-01', 20, 125_000),
                ('2026-10-01', 30, 99_999),
            ],
        )
        self.conn.commit()

        bot.handle_new_message(self.vk, self.conn, self.message('/легенды', uid=5))

        report = self.queued('send')[0]['message']
        self.assertIn('🌟 ЛЕГЕНДЫ ЧАТА 🌟', report)
        self.assertLess(report.index('1. [id20|Test User] — 125 000 сообщений'),
                        report.index('2. [id10|Test User] — 100 000 сообщений'))
        self.assertNotIn('@id30', report)
        self.assertTrue(report.endswith('Всего легенд: 2'))

    def test_legends_command_shows_empty_list_message(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/легенды'))
        self.assertEqual(
            self.queued('send')[0]['message'],
            'Пока в чате нет ни одной Легенды. Станьте первым — отправьте 100 000 сообщений!',
        )

    def test_profile_shows_donut_premium_only_when_active(self):
        event = {
            'event_id': 'premium-profile',
            'type': 'donut_subscription_create',
            'object': {'user_id': 1, 'amount': 50},
        }
        self.assertEqual(db.process_donut_event(self.conn, event, 50, now=self.now), 'activated')
        bot.handle_new_message(self.vk, self.conn, self.message('/profile', uid=1))
        profile = self.queued('send')[0]['message']
        self.assertIn('💎 Дон-премиум: активен', profile)

    def test_admin_can_grant_and_extend_one_month_premium_subscription(self):
        bot.handle_new_message(
            self.vk, self.conn, self.message('/подписка + @id2', uid=99, mid=1)
        )
        self.assertTrue(db.is_premium_user(self.conn, 2, now=self.now))
        notice = self.queued('send')[0]['message']
        self.assertIn('🎉 В чате новый премиум-участник!', notice)
        self.assertIn('[id2|Test User]', notice)
        self.assertIn('премиум-доступ на месяц', notice)

        expires_at = db.get_manual_premium_until(self.conn, 2)
        bot.handle_new_message(
            self.vk, self.conn, self.message('/подписка + @id2', uid=99, mid=2)
        )
        self.assertGreater(db.get_manual_premium_until(self.conn, 2), expires_at)
        self.assertIn('Премиум-подписка продлена', self.queued('send')[1]['message'])

        bot.handle_new_message(
            self.vk, self.conn, self.message('/profile @id2', uid=99, mid=3)
        )
        profile = self.queued('send')[2]['message']
        self.assertIn('🎁 Премиум от администрации: до ', profile)

    def test_non_admin_cannot_grant_premium(self):
        bot.handle_new_message(
            self.vk, self.conn, self.message('/подписка + @id2', uid=1)
        )
        self.assertFalse(db.is_premium_user(self.conn, 2, now=self.now))
        self.assertIn(
            'только администраторы',
            self.queued('send')[0]['message'],
        )

    def test_profile_daily_limits_separate_self_and_other_and_delete_excess_command(self):
        for index in range(2):
            bot.handle_new_message(self.vk, self.conn, self.message('/profile', uid=1, mid=index + 1))
        for index in range(2):
            bot.handle_new_message(self.vk, self.conn, self.message('/profile @id2', uid=1, mid=index + 3))
        bot.handle_new_message(self.vk, self.conn, self.message('/profile', uid=1, mid=5))

        self.assertEqual(len(self.queued('send')), 4)
        deletes = self.queued('delete')
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0]['params']['message_ids'], '5')

    def test_premium_characteristic_commands_limit_daily_and_per_target(self):
        event = {
            'event_id': 'premium-traits', 'type': 'donut_subscription_create',
            'object': {'user_id': 1, 'amount': config.PREMIUM_DONUT_MIN_AMOUNT_RUB},
        }
        db.process_donut_event(self.conn, event, config.PREMIUM_DONUT_MIN_AMOUNT_RUB, now=self.now)

        bot.handle_new_message(self.vk, self.conn, self.message('/токсик + @id2', uid=1, mid=1))
        bot.handle_new_message(self.vk, self.conn, self.message('/интеллект - @id2', uid=1, mid=2))
        bot.handle_new_message(self.vk, self.conn, self.message('/токсик - @id2', uid=1, mid=3))
        self.assertEqual(len(self.queued('delete')), 1)  # same trait to same recipient is limited weekly
        bot.handle_new_message(self.vk, self.conn, self.message('/токсик - @id3', uid=1, mid=4))
        bot.handle_new_message(self.vk, self.conn, self.message('/токсик + @id4', uid=1, mid=5))
        self.assertEqual(len(self.queued('delete')), 1)
        self.assertEqual(db.get_user_characteristics(self.conn, 2), {'токсик': 1, 'интеллект': -1})
        comments = [item['message'] for item in self.queued('send')]
        self.assertEqual(len(comments), 4)
        self.assertIn('💀 «Токсик»', comments[0])
        self.assertIn('(+1)', comments[0])
        self.assertIn('(−1)', comments[1])

        bot.handle_new_message(self.vk, self.conn, self.message('/profile @id2', uid=99, mid=6))
        profile = self.queued('send')[-1]['message']
        self.assertIn('💀 Токсик: +1', profile)
        self.assertIn('🧠 Интеллект: -1', profile)

    def test_regular_user_can_assign_one_characteristic_per_week(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/гений + @id2', uid=1, mid=1))
        bot.handle_new_message(self.vk, self.conn, self.message('/токсик - @id3', uid=1, mid=2))
        self.assertEqual(len(self.queued('delete')), 1)
        self.assertEqual(db.get_user_characteristics(self.conn, 2), {'гений': 1})

    def test_characteristic_cannot_be_assigned_to_self_even_by_admin(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/токсик + @id99', uid=99, mid=1))
        self.assertEqual(db.get_user_characteristics(self.conn, 99), {})
        self.assertIn('самому себе', self.queued('send')[0]['message'])

    def test_common_misspelling_of_gentleman_command_uses_same_trait(self):
        bot.handle_new_message(
            self.vk, self.conn,
            self.message('/джентельмен + @id2', uid=1, mid=1),
        )
        self.assertEqual(db.get_user_characteristics(self.conn, 2), {'джентльмен': 1})
        comment = self.queued('send')[0]['message']
        self.assertIn('🎩 «Джентльмен»', comment)
        self.assertIn('(+1)', comment)

    def test_flirt_and_heartbreaker_characteristic_commands(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/флиртёр + @id2', uid=99, mid=1))
        bot.handle_new_message(self.vk, self.conn, self.message('/сердцеед - @id3', uid=99, mid=2))
        bot.handle_new_message(self.vk, self.conn, self.message('/серцеед + @id4', uid=99, mid=3))
        bot.handle_new_message(self.vk, self.conn, self.message('/актёр + @id5', uid=99, mid=4))

        self.assertEqual(db.get_user_characteristics(self.conn, 2), {'флиртёр': 1})
        self.assertEqual(db.get_user_characteristics(self.conn, 3), {'сердцеед': -1})
        self.assertEqual(db.get_user_characteristics(self.conn, 4), {'сердцеед': 1})
        self.assertEqual(db.get_user_characteristics(self.conn, 5), {'актер': 1})
        comments = [item['message'] for item in self.queued('send')]
        self.assertIn('😏 «Флиртёр»', comments[0])
        self.assertIn('💘 «Сердцеед»', comments[1])
        self.assertIn('💘 «Сердцеед»', comments[2])
        self.assertIn('🎭 «Актёр»', comments[3])

    def test_new_characteristic_commands_from_updated_list(self):
        commands = (
            ('приколист', 'приколист'), ('шоумен', 'шоумен'),
            ('миротворец', 'миротворец'), ('хитрец', 'хитрец'),
            ('альфа', 'альфа'), ('бизнесмен', 'бизнесмен'),
            ('лизун', 'лизун'), ('дрочун', 'дрочун'), ('скуф', 'скуф'),
        )
        for index, (command, trait) in enumerate(commands, start=2):
            bot.handle_new_message(
                self.vk, self.conn,
                self.message(f'/{command} + @id{index}', uid=99, mid=index),
            )
            self.assertEqual(db.get_user_characteristics(self.conn, index), {trait: 1})

    def test_donut_longpoll_events_activate_cancel_and_expire_status(self):
        def event(event_type, event_id, **obj):
            raw = {
                'type': event_type,
                'event_id': event_id,
                'group_id': config.GROUP_ID,
                'object': obj,
            }
            return SimpleNamespace(type=event_type, raw=raw)

        created = event('donut_subscription_create', 'lp-1', user_id=77,
                        amount=config.PREMIUM_DONUT_MIN_AMOUNT_RUB)
        self.assertTrue(bot.handle_donut_event(self.conn, created))
        self.assertTrue(db.is_premium_user(self.conn, 77))
        self.assertTrue(bot.handle_donut_event(self.conn, created))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM processed_donut_events').fetchone()[0], 1)

        cancelled = event('donut_subscription_cancelled', 'lp-2', user_id=77)
        bot.handle_donut_event(self.conn, cancelled)
        self.assertTrue(db.is_premium_user(self.conn, 77))

        expired = event('donut_subscription_expired', 'lp-3', user_id=77)
        bot.handle_donut_event(self.conn, expired)
        self.assertFalse(db.is_premium_user(self.conn, 77))

    def test_donut_longpoll_event_from_wrong_group_is_ignored(self):
        event = SimpleNamespace(type='donut_subscription_create', raw={
            'type': 'donut_subscription_create', 'event_id': 'wrong-group',
            'group_id': config.GROUP_ID + 1,
            'object': {'user_id': 88, 'amount': 50},
        })
        self.assertTrue(bot.handle_donut_event(self.conn, event))
        self.assertFalse(db.is_premium_user(self.conn, 88))

    def test_message_rank_thresholds_are_inclusive_and_non_overlapping(self):
        cases = (
            (0, 'Новичок'),
            (9_999, 'Новичок'),
            (10_000, 'Участник'),
            (20_000, 'Читатель'),
            (30_000, 'Болтун'),
            (40_000, 'Активный'),
            (50_000, 'Флирт-мастер'),
            (60_000, 'Душа чата'),
            (70_000, 'Опытный'),
            (80_000, 'Ветеран любви'),
            (99_999, 'Ветеран любви'),
            (100_000, 'Легенда чата'),
            (250_000, 'Легенда чата'),
        )
        for total, expected in cases:
            with self.subTest(total=total):
                self.assertEqual(bot.chat_rank_for_messages(total), expected)

    def test_rank_up_is_announced_once_when_message_crosses_threshold(self):
        self.conn.execute(
            "INSERT INTO daily_user_stats(stat_date, user_id, messages) VALUES ('2025-01-01', 1, 9999)"
        )
        self.conn.commit()
        bot.handle_new_message(self.vk, self.conn, self.message('новое сообщение'))

        sends = self.queued('send')
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]['peer_id'], config.CHAT_PEER_ID)
        self.assertIn('🎖️ [id1|Test User] достиг ранга «Участник»!', sends[0]['message'])
        self.assertIn('мут нарушителя на 2 часа', sends[0]['message'])
        self.assertIn('1 раз за всё время на этом ранге', sends[0]['message'])

        bot.handle_new_message(self.vk, self.conn, self.message('ещё одно сообщение', mid=2))
        self.assertEqual(len(self.queued('send')), 1)

    def test_user_with_existing_rank_gets_no_retroactive_announcement(self):
        self.conn.execute(
            "INSERT INTO daily_user_stats(stat_date, user_id, messages) VALUES ('2025-01-01', 1, 20000)"
        )
        self.conn.commit()
        bot.handle_new_message(self.vk, self.conn, self.message('я давно в чате'))
        self.assertEqual(self.queued('send'), [])

    def test_legend_rank_announcement_includes_ten_hour_mute_right(self):
        self.conn.execute(
            "INSERT INTO daily_user_stats(stat_date, user_id, messages) VALUES ('2025-01-01', 1, 99999)"
        )
        self.conn.commit()
        bot.handle_new_message(self.vk, self.conn, self.message('ещё одно сообщение'))
        notice = self.queued('send')[0]['message']
        self.assertIn('«Легенда чата»', notice)
        self.assertIn('мут нарушителя на 10 часов', notice)

    def test_nonhero_without_rank_right_has_mute_command_deleted_silently(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/мут + @id2'))
        self.assertEqual(self.queued('send'), [])
        self.assertEqual(len(self.queued('delete')), 1)
        self.assertEqual(self.queued('mute'), [])

    def test_rank_mute_is_one_time_per_rank_and_uses_hourly_segments(self):
        self.conn.execute(
            "INSERT INTO daily_user_stats(stat_date, user_id, messages) VALUES ('2025-01-01', 1, 10000)"
        )
        self.conn.commit()
        bot.handle_new_message(self.vk, self.conn, self.message('/мут + @id2'))
        mutes = self.queued('mute')
        self.assertEqual(len(mutes), 2)
        self.assertEqual([item['until'] - item['requested_at'] for item in mutes], [3600, 3600])
        self.assertEqual([item['rank_notice'] for item in mutes], [True, False])
        self.assertEqual(db.get_command_cooldown(self.conn, 1, 'rank_mute:Участник'), 0)

        bot.deliver_outbox(self.vk, self.conn, self.now)
        self.assertEqual(db.get_command_cooldown(self.conn, 1, 'rank_mute:Участник'), self.now)
        bot.handle_new_message(self.vk, self.conn, self.message('/мут + @id3', mid=2))
        self.assertEqual(len(self.queued('mute')), 2)
        self.assertTrue(any(
            'Вы уже использовали мут на этом ранге. Дождитесь повышения.' in item['message']
            for item in self.queued('send')
        ))

    def test_unconfirmed_old_rank_mute_burns_when_user_advances(self):
        self.conn.execute(
            "INSERT INTO daily_user_stats(stat_date, user_id, messages) VALUES ('2025-01-01', 1, 19999)"
        )
        self.conn.commit()
        bot.apply_mute(
            self.vk, config.CHAT_PEER_ID, 2, now=self.now,
            rank_user_id=1, rank_title='Участник',
            rank_cooldown_key='rank_mute:Участник', hourly_segments=2,
        )

        bot.handle_new_message(self.vk, self.conn, self.message('порог нового ранга'))

        statuses = [row['status'] for row in self.conn.execute(
            "SELECT status FROM outbox WHERE kind = 'mute' ORDER BY id"
        )]
        self.assertEqual(statuses, ['cancelled', 'cancelled'])
        self.assertEqual(db.get_command_cooldown(self.conn, 1, 'rank_mute:Участник'), 0)
        self.assertIn('мут нарушителя на 3 часа', self.queued('send')[0]['message'])

    def test_rank_cannot_mute_higher_rank_or_current_hero(self):
        self.conn.execute(
            "INSERT INTO daily_user_stats(stat_date, user_id, messages) VALUES "
            "('2025-01-01', 1, 10000), ('2025-01-01', 2, 20000)"
        )
        self.conn.commit()
        bot.handle_new_message(self.vk, self.conn, self.message('/мут + @id2'))
        self.assertEqual(self.queued('mute'), [])

        today = datetime.fromtimestamp(self.now, config.CHAT_TZ).date()
        db.save_hero_title(self.conn, 'day', today.isoformat(), 3, 100)
        bot.handle_new_message(self.vk, self.conn, self.message('/мут + @id3', mid=2))
        self.assertEqual(self.queued('mute'), [])
        self.assertTrue(any('участника с более высоким рангом' in item['message']
                            for item in self.queued('send')))
        self.assertTrue(any('Нельзя мутить действующего Героя' in item['message']
                            for item in self.queued('send')))

    def test_admin_can_add_and_remove_social_chat_blacklist_entry(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/чс @kenaya', uid=99))
        self.assertTrue(db.is_chat_blacklisted(self.conn, 10))
        self.vk.messages.removeChatUser.assert_called_once_with(chat_id=2, user_id=10)
        self.assertIn('добавлен в ЧС', self.queued('send')[0]['message'])
        self.assertEqual(self.queued('send')[0]['delay'], 300)

        bot.handle_new_message(self.vk, self.conn, self.message('/чс снять @kenaya', uid=99, mid=2))
        self.assertFalse(db.is_chat_blacklisted(self.conn, 10))
        self.assertIn('удалён из чёрного списка', self.queued('send')[-1]['message'])
        self.assertEqual(self.queued('send')[-1]['delay'], 120)

    def test_admin_ban_unban_and_list_commands(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/бан + @kenaya', uid=99))
        self.assertTrue(db.is_chat_blacklisted(self.conn, 10))
        self.vk.messages.removeChatUser.assert_called_once_with(chat_id=2, user_id=10)
        self.assertIn('забанен и удалён из чата', self.queued('send')[-1]['message'])

        bot.handle_new_message(self.vk, self.conn, self.message('/баны', uid=99, mid=2))
        self.assertIn('@id10', self.queued('send')[-1]['message'])

        bot.handle_new_message(self.vk, self.conn, self.message('/разбан + @kenaya', uid=99, mid=3))
        self.assertFalse(db.is_chat_blacklisted(self.conn, 10))
        self.assertIn('@id10 разбанен', self.queued('send')[-1]['message'])

    def test_ban_commands_are_admin_only(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/бан + @kenaya', uid=1))
        self.assertFalse(db.is_chat_blacklisted(self.conn, 10))
        self.vk.messages.removeChatUser.assert_not_called()
        self.assertIn('только администраторам', self.queued('send')[0]['message'])

    def test_non_admin_cannot_change_blacklist(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/чс @kenaya', uid=1))
        self.assertFalse(db.is_chat_blacklisted(self.conn, 10))
        self.vk.messages.removeChatUser.assert_not_called()
        self.assertIn('только администраторам', self.queued('send')[0]['message'])

    def test_four_identical_messages_mute_only_in_social_chat(self):
        for mid in range(1, 4):
            bot.handle_new_message(
                self.vk, self.conn,
                self.message('одно и то же', mid=mid, peer=config.CHAT_PEER_ID),
            )
        self.assertEqual(self.queued('mute'), [])

        bot.handle_new_message(
            self.vk, self.conn,
            self.message('одно и то же', mid=4, peer=config.CHAT_PEER_ID),
        )
        mute = self.queued('mute')[0]
        self.assertEqual(mute['peer_id'], config.CHAT_PEER_ID)
        self.assertEqual(mute['until'] - self.now, 3600)
        self.assertEqual(
            mute['reason'],
            'Спам запрещен! Сообщение удалено! Доступ ограничен на 1 час!',
        )
        self.assertEqual(len(self.queued('delete')), 1)
        self.drain()
        self.assertTrue(any(
            item['message'] == (
                'Test User, Спам запрещен! Сообщение удалено! '
                'Доступ ограничен на 1 час!'
            )
            for item in self.queued('send')
        ))

        self.vk.messages.delete.reset_mock()
        bot.handle_new_message(
            self.vk, self.conn,
            self.message('одно и то же', peer=config.MODERATION_CHAT_PEER_ID, mid=5),
        )
        self.assertEqual(len(self.queued('mute')), 1)

    def test_links_are_deleted_and_muted_only_in_strict_chat_with_reason(self):
        message = self.message(
            'Переходите сюда https://example.com/page',
            peer=config.MODERATION_CHAT_PEER_ID,
        )
        bot.handle_new_message(self.vk, self.conn, message)
        mute = self.queued('mute')[0]
        self.assertEqual(mute['peer_id'], config.MODERATION_CHAT_PEER_ID)
        self.assertEqual(mute['until'] - self.now, 3600)
        self.assertEqual(mute['reason'], config.LINK_MUTE_REASON)
        self.assertEqual(len(self.queued('delete')), 1)

        bot.handle_new_message(
            self.vk, self.conn,
            self.message('https://example.com', peer=config.CHAT_PEER_ID, mid=2),
        )
        self.assertEqual(len(self.queued('mute')), 1)

    def test_strict_chat_mute_notice_stays_visible_for_one_hour(self):
        bot.send_mute_reason(
            self.vk, 1, config.PROHIBITED_WORDS_MUTE_REASON,
            config.MODERATION_CHAT_PEER_ID,
        )
        notice = self.queued('send')[0]
        self.assertEqual(notice['peer_id'], config.MODERATION_CHAT_PEER_ID)
        self.assertTrue(notice['temporary'])
        self.assertEqual(notice['delay'], 3600)

    def test_link_attachment_is_moderated_in_strict_chat(self):
        bot.handle_new_message(self.vk, self.conn, self.message(
            '', peer=config.MODERATION_CHAT_PEER_ID,
            attachments=[{'type': 'link', 'link': {'url': 'https://example.com'}}],
        ))
        self.assertEqual(self.queued('mute')[0]['reason'], config.LINK_MUTE_REASON)

    def test_requested_prohibited_phrases_mute_in_strict_chat(self):
        self.assertEqual(
            config.PROHIBITED_WORDS_MUTE_REASON,
            "Пожалуйста, избегайте обсуждения финансовых вопросов в этом чате.",
        )
        for mid, text in enumerate(
            ('Без предоплат', 'расход', 'на коммерческой основе', 'деньги'), 1
        ):
            bot.handle_new_message(self.vk, self.conn, self.message(
                text, uid=mid, peer=config.MODERATION_CHAT_PEER_ID, mid=mid,
            ))
        mutes = self.queued('mute')
        self.assertEqual(len(mutes), 4)
        self.assertTrue(all(item['until'] - self.now == 3600 for item in mutes))
        self.assertTrue(all(
            item['reason'] == config.PROHIBITED_WORDS_MUTE_REASON for item in mutes
        ))

    def test_different_message_breaks_identical_message_streak(self):
        for mid in (1, 2):
            bot.handle_new_message(self.vk, self.conn, self.message('повтор', mid=mid))
        bot.handle_new_message(self.vk, self.conn, self.message('другое', mid=3))
        for mid in (4, 5):
            bot.handle_new_message(self.vk, self.conn, self.message('повтор', mid=mid))
        self.assertEqual(self.queued('mute'), [])

    def test_at_all_message_is_deleted_without_mute(self):
        bot.handle_new_message(self.vk, self.conn, self.message('всем привет @all'))
        self.assertEqual(len(self.queued('delete')), 1)
        self.assertEqual(self.queued('mute'), [])

    def test_four_identical_consecutive_stickers_mute_and_queue_reason(self):
        for mid in range(1, 5):
            bot.handle_new_message(self.vk, self.conn, self.message(
                '', mid=mid,
                attachments=[{'type': 'sticker', 'sticker': {'sticker_id': 77, 'product_id': 1}}],
            ))
        mute = self.queued('mute')[0]
        self.assertEqual(
            mute['reason'],
            'Спам запрещен! Сообщение удалено! Доступ ограничен на 1 час!',
        )
        self.assertEqual(mute['until'] - self.now, 3600)
        self.assertEqual(len(self.queued('delete')), 1)
        self.drain()
        self.assertTrue(any(
            item['message'] == (
                'Test User, Спам запрещен! Сообщение удалено! '
                'Доступ ограничен на 1 час!'
            )
            for item in self.queued('send')
        ))
        self.assertEqual(
            self.conn.execute('SELECT delete_at FROM bot_messages').fetchone()[0],
            self.now + 300,
        )

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
        self.assertEqual(self.queued('send'), [])

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
        self.assertTrue(self.queued('send')[-1]['message'].startswith('👤 [id1|Test User]'))

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

    def test_premium_reputation_allows_three_people_per_day_and_each_once_per_week(self):
        event = {
            'event_id': 'premium-reputation', 'type': 'donut_subscription_create',
            'object': {'user_id': 1, 'amount': config.PREMIUM_DONUT_MIN_AMOUNT_RUB},
        }
        db.process_donut_event(self.conn, event, config.PREMIUM_DONUT_MIN_AMOUNT_RUB, now=self.now)
        bot.handle_new_message(self.vk, self.conn, self.message('/rep + @id2', uid=1, mid=1))
        bot.handle_new_message(self.vk, self.conn, self.message('/rep - @id2', uid=1, mid=2))
        bot.handle_new_message(self.vk, self.conn, self.message('/rep - @id3', uid=1, mid=3))
        bot.handle_new_message(self.vk, self.conn, self.message('/rep @id4', uid=1, mid=4))
        bot.handle_new_message(self.vk, self.conn, self.message('/rep @id5', uid=1, mid=5))
        bot.handle_new_message(self.vk, self.conn, self.message('/rep @id1', uid=1, mid=6))

        self.assertEqual(db.get_reputation(self.conn, 2), 1)
        self.assertEqual(db.get_reputation(self.conn, 3), -1)
        self.assertEqual(db.get_reputation(self.conn, 4), 1)
        self.assertEqual(db.get_reputation(self.conn, 5), 0)
        self.assertEqual(db.get_reputation(self.conn, 1), 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM reputation_votes WHERE giver_id = 1').fetchone()[0], 3)
        self.assertEqual(len(self.queued('send')), 4)

    def test_admin_reputation_is_unlimited_but_self_vote_is_forbidden(self):
        bot.handle_new_message(self.vk, self.conn, self.message('/rep + @id2', uid=99, mid=1))
        bot.handle_new_message(self.vk, self.conn, self.message('/rep - @id2', uid=99, mid=2))
        bot.handle_new_message(self.vk, self.conn, self.message('/rep + @id99', uid=99, mid=3))
        self.assertEqual(db.get_reputation(self.conn, 2), 0)
        self.assertEqual(db.get_reputation(self.conn, 99), 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM reputation_votes WHERE giver_id = 99').fetchone()[0], 2)

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
        self.conn.executemany(
            "INSERT INTO chat_stat_records(metric, record_value, stat_date) VALUES (?, ?, ?)",
            [
                ('active_users', 72, '2026-10-16'),
                ('daily_messages', 1900, '2026-10-07'),
            ],
        )
        self.conn.commit()
        with patch.object(db, 'get_daily_stats', return_value=stats):
            self.assertTrue(bot.publish_daily_stats(
                self.vk, self.conn, stat_date, include_records=True
            ))

        report = self.queued('send')[0]['message']
        self.assertIn('👥 Активных участников: 18', report)
        self.assertIn('⚡ Активность чата: 150% от среднего за 7 дней', report)
        self.assertIn('👥 Рекорд активных участников: 72 (16.10.2026)', report)
        self.assertIn('⚡ Рекорд активности чата: 1900 сообщений за день (07.10.2026)', report)

    def test_intraday_daily_report_does_not_include_all_time_records(self):
        stat_date = datetime.fromtimestamp(self.now, config.CHAT_TZ).date()
        stats = {
            'date': stat_date.isoformat(), 'messages': 3, 'active_users': 1,
            'activity_percent': None, 'activity_average_messages': 0,
            'photos': 0, 'videos': 0, 'music': 0, 'voices': 0, 'top_users': [],
        }
        with patch.object(db, 'get_daily_stats', return_value=stats):
            self.assertTrue(bot.publish_daily_stats(self.vk, self.conn, stat_date))
        report = self.queued('send')[0]['message']
        self.assertNotIn('Рекорд активных участников:', report)
        self.assertNotIn('Рекорд активности чата:', report)

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
        self.assertEqual(self.conn.execute('SELECT delete_at FROM bot_messages').fetchone()[0], self.now + 3605)

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
        king_notice = next(item for item in self.queued('send')
                           if item['message'].startswith('🏅 Герой дня выдал мут'))
        self.assertTrue(king_notice['temporary'])
        self.assertEqual(king_notice['delay'], 300)
        self.assertEqual(king_notice['peer_id'], config.CHAT_PEER_ID)
        bot.handle_new_message(self.vk, self.conn, self.message('/мут @id4', mid=3))
        self.assertEqual(len(self.queued('mute')), 1)

    def test_week_hero_applies_four_hourly_mutes_and_announces_once(self):
        today = datetime.fromtimestamp(self.now, config.CHAT_TZ).date()
        monday = today - timedelta(days=today.weekday())
        db.save_hero_title(self.conn, 'week', monday.isoformat(), 1, 40)
        bot.handle_new_message(self.vk, self.conn, self.message('/мут @id2'))
        mutes = self.queued('mute')
        self.assertEqual(len(mutes), 4)
        self.assertEqual([item['until'] - item['requested_at'] for item in mutes], [3600] * 4)
        self.assertEqual([item['hero_notice'] for item in mutes], [True, False, False, False])

        bot.deliver_outbox(self.vk, self.conn, self.now)
        self.assertEqual(self.vk.messages.changeConversationMemberRestrictions.call_count, 1)
        bot.deliver_outbox(self.vk, self.conn, self.now + 5)
        self.assertEqual(self.vk.messages.send.call_count, 1)
        for hour in (1, 2, 3):
            bot.deliver_outbox(self.vk, self.conn, self.now + hour * 3600)
        self.assertEqual(self.vk.messages.changeConversationMemberRestrictions.call_count, 4)
        self.assertEqual(self.vk.messages.send.call_count, 1)
        self.assertEqual(db.mute_deadline(self.conn, config.CHAT_PEER_ID, 2), self.now + 4 * 3600)

    def test_senior_hero_status_overrides_junior_mute_right(self):
        today = datetime.fromtimestamp(self.now, config.CHAT_TZ).date()
        monday = today - timedelta(days=today.weekday())
        for period_type, period_key in (
            ('day', today.isoformat()), ('week', monday.isoformat()),
            ('month', today.strftime('%Y-%m')),
        ):
            db.save_hero_title(self.conn, period_type, period_key, 1, 20)
        bot.handle_new_message(self.vk, self.conn, self.message('/мут @id2'))
        mutes = self.queued('mute')
        self.assertEqual(len(mutes), 1)
        payload = mutes[0]
        self.assertEqual(payload['hero_period_type'], 'month')
        self.assertEqual(payload['until'] - self.now, 8 * 3600)

    def test_premium_hero_can_mute_another_premium_user(self):
        today = datetime.fromtimestamp(self.now, config.CHAT_TZ).date()
        db.save_king(self.conn, today - timedelta(days=1), 1, 20)
        for user_id, event_id in ((1, 'don-hero'), (2, 'don-target')):
            event = {
                'event_id': event_id,
                'type': 'donut_subscription_create',
                'object': {'user_id': user_id, 'amount': config.PREMIUM_DONUT_MIN_AMOUNT_RUB},
            }
            self.assertEqual(
                db.process_donut_event(self.conn, event, config.PREMIUM_DONUT_MIN_AMOUNT_RUB,
                                       now=self.now),
                'activated',
            )

        bot.handle_new_message(self.vk, self.conn, self.message('/мут @id2'))

        self.assertEqual(len(self.queued('mute')), 1)
        self.assertEqual(len(self.queued('send')), 0)
        self.drain()
        self.assertEqual(self.vk.messages.changeConversationMemberRestrictions.call_count, 1)
        notice = self.queued('send')[0]
        self.assertIn('Герой дня выдал мут пользователю [id2|Test User]', notice['message'])

    def test_hero_can_mute_an_administrator(self):
        today = datetime.fromtimestamp(self.now, config.CHAT_TZ).date()
        db.save_king(self.conn, today - timedelta(days=1), 1, 20)
        bot.handle_new_message(self.vk, self.conn, self.message('/мут @id99'))
        self.assertEqual(len(self.queued('mute')), 1)

    def test_nonpremium_hero_can_mute_premium_user(self):
        today = datetime.fromtimestamp(self.now, config.CHAT_TZ).date()
        db.save_king(self.conn, today - timedelta(days=1), 1, 20)
        event = {
            'event_id': 'don-target-only',
            'type': 'donut_subscription_create',
            'object': {'user_id': 2, 'amount': config.PREMIUM_DONUT_MIN_AMOUNT_RUB},
        }
        db.process_donut_event(self.conn, event, config.PREMIUM_DONUT_MIN_AMOUNT_RUB, now=self.now)

        bot.handle_new_message(self.vk, self.conn, self.message('/мут @id2'))

        self.assertEqual(len(self.queued('mute')), 1)
        self.assertEqual(len(self.queued('send')), 0)

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

    def test_donut_promo_is_posted_once_at_noon(self):
        noon = datetime(2026, 10, 9, 12, 5, tzinfo=config.CHAT_TZ)
        bot.scheduler_tick(self.vk, self.conn, noon)
        bot.scheduler_tick(self.vk, self.conn, noon + timedelta(minutes=1))
        promos = [item['message'] for item in self.queued('send')
                  if item['message'].startswith('💎 Хочешь больше возможностей в чате?')]
        self.assertEqual(promos, [
            "💎 Хочешь больше возможностей в чате?\n"
            "Don-премиум открывает расширенные лимиты:\n"
            "• Профиль — 5 просмотров своего и 5 чужих профилей в день вместо 2 и 2.\n"
            "• Характеристики — до 5 разных участников в день вместо одной выдачи в неделю.\n"
            "• Репутация — до 3 разных участников в день вместо одного голоса в неделю.\n"
            "• 💍 /Свадьба — предложить и заключить брак могут два участника с активным Don, после согласия второго.\n"
            "• 💔 /Развод — расторгнуть свой брак без подтверждения партнёра.\n"
            "Подписка VK Donut @vlgchat - 100 ₽ в месяц. Её поддержка помогает нам развивать чат! "
            "Подробности - в разделе Donut сообщества. Спасибо!"
        ])

    def test_donut_promo_missed_by_more_than_30_minutes_is_skipped(self):
        late = datetime(2026, 10, 9, 12, 40, tzinfo=config.CHAT_TZ)
        bot.scheduler_tick(self.vk, self.conn, late)
        self.assertFalse(any(item['message'].startswith('💎 Хочешь больше возможностей в чате?')
                             for item in self.queued('send')))

    def test_scheduler_catches_midnight_king_and_weekly_report(self):
        sunday = datetime(2026, 10, 11, 23, 30, tzinfo=config.CHAT_TZ)
        db.record_message(self.conn, 42, int(sunday.timestamp()))
        # Establish checkpoint without generating first-start catchup output.
        db.claim_scheduler_event(self.conn, 'checkpoint', int(sunday.timestamp()))
        monday = sunday + timedelta(hours=14)
        bot.scheduler_tick(self.vk, self.conn, monday)
        self.assertEqual(db.get_king(self.conn, sunday.date()), (42, 1))
        self.assertEqual(db.get_hero_title(self.conn, 'week', '2026-10-12'), (42, 1))
        reports = [p['message'] for p in self.queued('send')]
        self.assertTrue(any('ГЕРОЙ ДНЯ' in text for text in reports))
        self.assertTrue(any('ИТОГИ НЕДЕЛИ' in text for text in reports))
        count = len(reports)
        bot.scheduler_tick(self.vk, self.conn, monday)
        self.assertEqual(len(self.queued('send')), count)
        hero_report = next(text for text in reports if 'ГЕРОЙ ДНЯ' in text)
        daily_report = next(text for text in reports if 'СТАТИСТИКА ЧАТА' in text)
        self.assertNotIn('Рекорд активных участников:', hero_report)
        self.assertNotIn('Рекорд активности чата:', hero_report)
        self.assertIn('Рекорд активных участников:', daily_report)
        self.assertIn('Рекорд активности чата:', daily_report)

    def test_scheduler_assigns_month_and_year_heroes_on_period_boundaries(self):
        previous_day = date(2025, 12, 31)
        for user_id, count, stat_day in (
            (42, 1, previous_day), (42, 8, date(2025, 6, 1)),
            (43, 3, date(2025, 8, 1)),
        ):
            start = int(datetime.combine(stat_day, datetime.min.time(), config.CHAT_TZ).timestamp())
            for offset in range(count):
                db.record_message(self.conn, user_id, start + 12 * 3600 + offset)
        before = datetime(2025, 12, 31, 23, 59, tzinfo=config.CHAT_TZ)
        db.claim_scheduler_event(self.conn, 'checkpoint', int(before.timestamp()))
        jan_first = datetime(2026, 1, 1, 0, 5, tzinfo=config.CHAT_TZ)
        bot.scheduler_tick(self.vk, self.conn, jan_first)
        self.assertEqual(db.get_hero_title(self.conn, 'month', '2026-01'), (42, 1))
        self.assertEqual(db.get_hero_title(self.conn, 'year', '2026'), (42, 9))

    def test_scheduler_does_not_assign_period_heroes_mid_period_on_first_install(self):
        today = datetime.fromtimestamp(self.now, config.CHAT_TZ).date()
        for offset in range(5):
            db.record_message(self.conn, 42, self.now - offset)
        bot.scheduler_tick(self.vk, self.conn, datetime.fromtimestamp(self.now, config.CHAT_TZ))
        monday = today - timedelta(days=today.weekday())
        self.assertIsNone(db.get_hero_title(self.conn, 'week', monday.isoformat()))
        self.assertIsNone(db.get_hero_title(self.conn, 'month', today.strftime('%Y-%m')))
        self.assertIsNone(db.get_hero_title(self.conn, 'year', today.strftime('%Y')))

    def test_midnight_hero_announcement_always_publishes_existing_records(self):
        stat_date = datetime.fromtimestamp(self.now, config.CHAT_TZ).date() - timedelta(days=1)
        record_date = stat_date - timedelta(days=1)
        db.save_king(self.conn, record_date, 42, 12)
        self.conn.execute(
            "INSERT INTO chat_stat_records(metric, record_value, stat_date) VALUES (?, ?, ?)",
            ('active_users', 9, record_date.isoformat()),
        )
        self.conn.execute(
            "INSERT INTO chat_stat_records(metric, record_value, stat_date) VALUES (?, ?, ?)",
            ('daily_messages', 175, record_date.isoformat()),
        )
        self.conn.commit()
        for _ in range(3):
            db.record_message(self.conn, 1, self.now - 24 * 60 * 60)

        self.assertTrue(bot.publish_king(self.vk, self.conn, stat_date))

        report = self.queued('send')[0]['message']
        self.assertNotIn('Рекорд активных участников:', report)
        self.assertNotIn('Рекорд активности чата:', report)
        self.assertIn('Рекорд Героя дня: [id42|Test User] — 12 сообщ. за день', report)
        self.assertNotIn('новый рекорд', report)

        self.assertTrue(bot.publish_daily_stats(self.vk, self.conn, stat_date, include_records=True))
        stats_report = self.queued('send')[1]['message']
        self.assertIn('Рекорд активных участников: 9', stats_report)
        self.assertIn('Рекорд активности чата: 175 сообщений за день', stats_report)

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
        self.assertEqual(sum('ГЕРОЙ ДНЯ' in p['message'] for p in self.queued('send')), 1)

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
        self.conn.execute('DELETE FROM schema_migrations WHERE version >= 10')
        self.conn.commit()
        original = db._migration_10
        def fail(conn):
            original(conn)
            conn.execute('CREATE TABLE temporary_migration_probe(id INTEGER)')
            raise RuntimeError('migration failed')
        with patch.object(db, '_migration_10', side_effect=fail):
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
        self.conn.execute('DELETE FROM schema_migrations WHERE version >= 10')
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
        bot.cleanup_tick(self.conn, self.now + 3600)
        self.vk.messages.delete.return_value = [{'peer_id': config.MODERATION_CHAT_PEER_ID,
            'conversation_message_id': 77, 'message_id': 0, 'response': 1}]
        bot.deliver_outbox(self.vk, self.conn, self.now + 3600)
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
