from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .models import (
    KnowledgeItem,
    ScheduleCategory,
    ScheduleEvent,
)
from .slack_digest import build_digest


class SlackDigestTests(TestCase):
    def setUp(self):
        self.waiting = KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key="slack:C1:100",
            status=KnowledgeItem.Status.AWAITING_ANSWER,
            title="배포 승인자는 누구인가요?",
            question="배포 승인자는 누구인가요?",
            source_hash="a" * 64,
            generated_at=timezone.now(),
            slack_channel_id="C1",
            slack_thread_ts="100.100",
        )

    def test_digest_summarizes_actionable_items_without_answering_them(self):
        category = ScheduleCategory.objects.get(name="기타")
        ScheduleEvent.objects.create(
            title="오래된 할 일",
            item_type=ScheduleEvent.ItemType.TODO,
            todo_category=category,
            starts_at=timezone.now() - timedelta(days=2),
        )

        digest = build_digest()

        self.assertIn("답변 대기: 1", digest.text)
        self.assertIn("기한이 지난 할 일: 1", digest.text)
        self.assertIn(self.waiting.title, digest.text)
        self.assertEqual(digest.actionable_count, 2)

    def test_digest_escapes_slack_control_mentions_in_titles(self):
        self.waiting.title = "<!channel> 배포 확인"
        self.waiting.save(update_fields=["title", "updated_at"])

        digest = build_digest()

        self.assertNotIn("<!channel>", digest.text)
        self.assertIn("&lt;!channel&gt;", digest.text)

    def test_dry_run_never_constructs_a_slack_client(self):
        output = StringIO()
        with patch(
            "dashboard.management.commands.send_slack_digest.WebClient"
        ) as client:
            call_command("send_slack_digest", dry_run=True, stdout=output)

        client.assert_not_called()
        self.assertIn("답변 대기: 1", output.getvalue())

    def test_explicit_send_posts_once(self):
        client = patch(
            "dashboard.management.commands.send_slack_digest.WebClient"
        ).start()
        self.addCleanup(patch.stopall)
        client.return_value.chat_postMessage.return_value = {"ts": "123.456"}

        call_command(
            "send_slack_digest",
            channel="C-DIGEST",
            slack_token="xoxb-test",
        )

        client.assert_called_once_with(token="xoxb-test")
        client.return_value.chat_postMessage.assert_called_once()
