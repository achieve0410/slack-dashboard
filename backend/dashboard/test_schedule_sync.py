from django.test import TestCase
from django.utils import timezone

from dashboard.management.commands.sync_slack import Command

from .models import ScheduleCategory, ScheduleEvent
from .schedule_sync import (
    infer_todo_category,
    parse_schedule_message,
    reconcile_schedule_channel,
)


class ScheduleMessageParserTests(TestCase):
    def test_parses_timed_range_and_notes(self):
        parsed = parse_schedule_message(
            "2026-07-20 14:00~15:00 | 주간 업무 회의 | 안건 정리"
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.title, "주간 업무 회의")
        self.assertEqual(parsed.notes, "안건 정리")
        self.assertFalse(parsed.all_day)
        self.assertEqual(timezone.localtime(parsed.starts_at).strftime("%F %R"), "2026-07-20 14:00")
        self.assertEqual(timezone.localtime(parsed.ends_at).strftime("%F %R"), "2026-07-20 15:00")

    def test_parses_all_day_and_leading_mention(self):
        parsed = parse_schedule_message(
            "<@U1234567890> 2026-07-21 종일 | 자격증 시험 접수"
        )

        self.assertIsNotNone(parsed)
        self.assertTrue(parsed.all_day)
        self.assertIsNone(parsed.ends_at)
        self.assertEqual(parsed.title, "자격증 시험 접수")

    def test_parses_undated_and_dated_todos_with_categories(self):
        undated = parse_schedule_message("TODO | 자료 검토하기 | 금요일 전 확인")
        dated = parse_schedule_message("2026-07-22 | 병원 예약")

        self.assertEqual(undated.item_type, ScheduleEvent.ItemType.TODO)
        self.assertEqual(
            undated.todo_category_id,
            ScheduleCategory.objects.get(name="업무").pk,
        )
        self.assertIsNone(undated.starts_at)
        self.assertEqual(undated.notes, "금요일 전 확인")
        self.assertEqual(dated.item_type, ScheduleEvent.ItemType.TODO)
        self.assertEqual(
            dated.todo_category_id,
            ScheduleCategory.objects.get(name="건강").pk,
        )
        self.assertTrue(dated.all_day)
        self.assertEqual(
            timezone.localtime(dated.starts_at).strftime("%F"),
            "2026-07-22",
        )

    def test_rejects_non_schedule_and_invalid_ranges(self):
        self.assertIsNone(parse_schedule_message("<@U1234567890> 초대"))
        self.assertIsNone(parse_schedule_message("2026-07-20 15:00~14:00 | 잘못된 일정"))
        self.assertIsNone(parse_schedule_message("2026-02-30 종일 | 없는 날짜"))

    def test_single_character_keyword_does_not_match_inside_another_word(self):
        self.assertEqual(
            infer_todo_category("겨울 여행 비행기 예약").name,
            "여행",
        )
        self.assertEqual(
            infer_todo_category("약 복용").name,
            "건강",
        )


class ScheduleChannelReconciliationTests(TestCase):
    channel_id = "C1234567890"
    bot_user_id = "U1234567890"

    def test_create_update_idempotency_and_delete(self):
        messages = [
            {
                "ts": "100.100",
                "user": "U123",
                "text": "2026-07-20 14:00~15:00 | 주간 업무 회의 | 안건 정리",
            },
            {
                "ts": "100.200",
                "thread_ts": "100.100",
                "user": "U123",
                "text": "스레드 답글",
            },
            {
                "ts": "100.300",
                "user": self.bot_user_id,
                "text": "2026-07-21 종일 | 봇 메시지",
            },
            {
                "ts": "100.400",
                "user": "U123",
                "subtype": "channel_join",
                "text": "채널에 참여했습니다.",
            },
        ]

        created = reconcile_schedule_channel(self.channel_id, messages, self.bot_user_id)
        self.assertEqual(created, {"created": 1, "updated": 0, "unchanged": 0, "deleted": 0, "skipped": 3})
        event = ScheduleEvent.objects.get()
        event.completed = True
        event.save(update_fields=["completed"])

        unchanged = reconcile_schedule_channel(self.channel_id, messages, self.bot_user_id)
        self.assertEqual(unchanged["unchanged"], 1)
        self.assertEqual(ScheduleEvent.objects.count(), 1)

        messages[0]["text"] = "2026-07-20 15:00~16:00 | 수정된 회의 | 새 안건"
        updated = reconcile_schedule_channel(self.channel_id, messages, self.bot_user_id)
        event.refresh_from_db()
        self.assertEqual(updated["updated"], 1)
        self.assertEqual(event.title, "수정된 회의")
        self.assertTrue(event.completed)

        deleted = reconcile_schedule_channel(self.channel_id, messages[1:], self.bot_user_id)
        self.assertEqual(deleted["deleted"], 1)
        self.assertFalse(ScheduleEvent.objects.exists())

    def test_fetch_channel_reads_all_pages_and_sorts_messages(self):
        class Client:
            def __init__(self):
                self.calls = []

            def conversations_history(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return {
                        "messages": [{"ts": "200.200"}],
                        "response_metadata": {"next_cursor": "next"},
                    }
                return {
                    "messages": [{"ts": "100.100"}],
                    "response_metadata": {"next_cursor": ""},
                }

        client = Client()

        messages = Command.fetch_channel(client, self.channel_id)

        self.assertEqual([message["ts"] for message in messages], ["100.100", "200.200"])
        self.assertEqual(client.calls[0], {"channel": self.channel_id, "limit": 200})
        self.assertEqual(
            client.calls[1],
            {"channel": self.channel_id, "limit": 200, "cursor": "next"},
        )

    def test_reconciles_slack_todo(self):
        stats = reconcile_schedule_channel(
            self.channel_id,
            [{"ts": "300.100", "user": "U123", "text": "TODO | 영어 복습"}],
            self.bot_user_id,
        )

        event = ScheduleEvent.objects.get()
        self.assertEqual(stats["created"], 1)
        self.assertEqual(event.item_type, ScheduleEvent.ItemType.TODO)
        self.assertEqual(event.todo_category.name, "학습")
        self.assertIsNone(event.starts_at)

    def test_preserves_manually_selected_category_during_slack_sync(self):
        messages = [{"ts": "400.100", "user": "U123", "text": "TODO | 자료 검토"}]
        reconcile_schedule_channel(self.channel_id, messages, self.bot_user_id)
        event = ScheduleEvent.objects.get()
        event.todo_category = ScheduleCategory.objects.get(name="여행")
        event.todo_category_manual = True
        event.save(update_fields=["todo_category", "todo_category_manual"])

        reconcile_schedule_channel(self.channel_id, messages, self.bot_user_id)

        event.refresh_from_db()
        self.assertEqual(event.todo_category.name, "여행")
        self.assertTrue(event.todo_category_manual)
