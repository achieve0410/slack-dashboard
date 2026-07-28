import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from . import llm
from .management.commands.sync_slack import Command
from .models import (
    Category,
    ContentRun,
    CronJob,
    KnowledgeFeedback,
    KnowledgeItem,
    LLMUsageRecord,
    QuizDomainConfig,
)
from .services import reconcile_cron_runs


NOW = datetime(2026, 7, 28, 1, 0, tzinfo=UTC)


def create_item(*, title="운영 지식", verified=False) -> KnowledgeItem:
    job = CronJob.objects.create(external_id=f"test:{title}", name=title)
    run = ContentRun.objects.create(
        job=job,
        external_ts=f"{job.pk}.100",
        status=ContentRun.Status.SUCCESS,
        title=title,
        body=f"{title} 본문",
        generated_at=NOW,
    )
    reconcile_cron_runs([run.pk])
    item = KnowledgeItem.objects.get(content_run=run)
    if verified:
        category = Category.objects.create(
            name="운영",
            path=f"운영{job.pk}",
            parent=None,
            depth=1,
        )
        item.status = KnowledgeItem.Status.CLASSIFIED
        item.category = category
        item.classified_at = NOW
        item.verification_status = KnowledgeItem.VerificationStatus.VERIFIED
        item.verified_at = NOW
        item.review_due_at = NOW + timedelta(days=30)
        item.save()
    return item


class VerificationApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="owner",
            password="test-password",
        )
        self.client.force_login(self.user)
        self.item = create_item()

    def test_verifies_item_and_defaults_review_due_date(self):
        response = self.client.patch(
            f"/api/knowledge/{self.item.pk}/verification/",
            data={"status": "verified", "note": "원문 확인"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.assertEqual(
            self.item.verification_status,
            KnowledgeItem.VerificationStatus.VERIFIED,
        )
        self.assertEqual(self.item.verification_owner, self.user)
        self.assertIsNotNone(self.item.verified_at)
        self.assertIsNotNone(self.item.review_due_at)
        self.assertEqual(response.json()["note"], "원문 확인")

    def test_outdated_feedback_marks_item_stale(self):
        response = self.client.post(
            f"/api/knowledge/{self.item.pk}/feedback/",
            data={"kind": "outdated", "comment": "정책이 변경됨"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        self.item.refresh_from_db()
        self.assertEqual(
            self.item.verification_status,
            KnowledgeItem.VerificationStatus.STALE,
        )
        self.assertEqual(KnowledgeFeedback.objects.get().created_by, self.user)
        self.assertEqual(response.json()["verification"]["feedback_counts"]["outdated"], 1)

    def test_expired_review_is_reported_as_stale(self):
        self.item.verification_status = KnowledgeItem.VerificationStatus.VERIFIED
        self.item.verified_at = timezone.now() - timedelta(days=100)
        self.item.review_due_at = timezone.now() - timedelta(days=1)
        self.item.save()

        response = self.client.get(f"/api/knowledge/{self.item.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["verification"]["status"], "stale")

    def test_cron_run_detail_exposes_same_verification_payload(self):
        response = self.client.get(f"/api/runs/{self.item.content_run_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["knowledge_item"]["verification"]["status"],
            "unverified",
        )


class IncrementalSlackSyncTests(TestCase):
    class Client:
        def __init__(self, batches):
            self.batches = list(batches)
            self.calls = []

        def conversations_info(self, **_kwargs):
            return {"channel": {"name": "knowledge"}}

        def conversations_history(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "messages": self.batches.pop(0),
                "response_metadata": {"next_cursor": ""},
            }

    def test_reuses_saved_cursor_on_next_sync(self):
        client = self.Client(
            [
                [{"ts": "100.100", "text": "첫 번째"}],
                [{"ts": "200.200", "text": "두 번째"}],
            ]
        )
        command = Command()

        first = command.sync_knowledge_channel(client, "C1", "B1")
        second = command.sync_knowledge_channel(client, "C1", "B1")

        self.assertEqual(first.cursor_ts, "100.100")
        self.assertEqual(second.cursor_ts, "200.200")
        self.assertNotIn("oldest", client.calls[0])
        self.assertEqual(client.calls[1]["oldest"], "100.100")
        self.assertFalse(client.calls[1]["inclusive"])
        job = CronJob.objects.get(external_id="channel:C1")
        self.assertEqual(job.sync_cursor_ts, "200.200")
        self.assertEqual(job.last_import_count, 1)

    def test_full_rescan_hides_deleted_source_and_restores_reappearing_message(self):
        client = self.Client(
            [
                [{"ts": "100.100", "text": "원문"}],
                [],
                [{"ts": "100.100", "text": "원문"}],
            ]
        )
        command = Command()
        command.sync_knowledge_channel(client, "C1", "B1")

        deleted = command.sync_knowledge_channel(
            client,
            "C1",
            "B1",
            full_rescan=True,
        )
        run = ContentRun.objects.get(external_ts="100.100")
        item = KnowledgeItem.objects.get(content_run=run)
        self.assertEqual(deleted.deleted, 1)
        self.assertIsNotNone(run.hidden_at)
        self.assertIsNotNone(item.hidden_at)

        command.sync_knowledge_channel(
            client,
            "C1",
            "B1",
            full_rescan=True,
        )
        run.refresh_from_db()
        item.refresh_from_db()
        self.assertIsNone(run.hidden_at)
        self.assertIsNone(item.hidden_at)

    def test_incremental_sync_preserves_item_only_manual_hide(self):
        client = self.Client(
            [
                [{"ts": "100.100", "text": "원문"}],
                [{"ts": "100.100", "text": "원문 수정"}],
            ]
        )
        command = Command()
        command.sync_knowledge_channel(client, "C1", "B1")
        item = KnowledgeItem.objects.get(content_run__external_ts="100.100")
        item.hidden_at = NOW
        item.save(update_fields=["hidden_at", "updated_at"])

        command.sync_knowledge_channel(
            client,
            "C1",
            "B1",
            full_rescan=True,
        )

        item.refresh_from_db()
        self.assertEqual(item.hidden_at, NOW)

    def test_cursor_does_not_advance_when_knowledge_reconciliation_fails(self):
        client = self.Client(
            [[{"ts": "100.100", "text": "재시도해야 하는 원문"}]]
        )
        command = Command()

        with patch(
            "dashboard.management.commands.sync_slack.reconcile_cron_runs",
            side_effect=RuntimeError("reconcile failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "reconcile failed"):
                command.sync_knowledge_channel(client, "C1", "B1")

        job = CronJob.objects.get(external_id="channel:C1")
        self.assertEqual(job.sync_cursor_ts, "")
        self.assertEqual(job.last_import_count, 0)


class LLMBudgetTests(TestCase):
    def test_records_usage_and_blocks_after_daily_call_limit(self):
        config = llm.LLMConfig(
            provider="anthropic",
            model="test-model",
            api_key="test-key",
            max_tokens=100,
        )
        response = llm.LLMResponse(
            text='{"ok":true}',
            usage={
                "model": "test-model",
                "provider": "anthropic",
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "api_calls": 1,
            },
        )
        environment = {
            "LLM_DAILY_API_CALL_LIMIT": "1",
            "LLM_DAILY_TOKEN_LIMIT": "0",
            "LLM_DAILY_COST_USD_LIMIT": "0",
            "LLM_INPUT_COST_PER_MTOK_USD": "3",
            "LLM_OUTPUT_COST_PER_MTOK_USD": "15",
        }
        with patch.dict(os.environ, environment, clear=False), patch(
            "dashboard.llm._anthropic_complete",
            return_value=response,
        ):
            llm.complete(
                config,
                "{}",
                timeout=10,
                operation="ask",
            )
            with self.assertRaisesRegex(llm.LLMConfigError, "daily_budget_exceeded"):
                llm.complete(
                    config,
                    "{}",
                    timeout=10,
                    operation="ask",
                )

        record = LLMUsageRecord.objects.get()
        self.assertEqual(record.total_tokens, 15)
        self.assertEqual(record.operation, "ask")
        self.assertGreater(record.estimated_cost_usd, 0)

    def test_invalid_cost_rate_is_rejected_before_provider_call(self):
        config = llm.LLMConfig(
            provider="anthropic",
            model="test-model",
            api_key="test-key",
            max_tokens=100,
        )
        with patch.dict(
            os.environ,
            {"LLM_INPUT_COST_PER_MTOK_USD": "invalid"},
            clear=False,
        ), patch("dashboard.llm._anthropic_complete") as provider_call:
            with self.assertRaisesRegex(llm.LLMConfigError, "invalid_budget"):
                llm.complete(
                    config,
                    "{}",
                    timeout=10,
                    operation="ask",
                )

        provider_call.assert_not_called()

    def test_dashboard_reports_invalid_budget_without_failing(self):
        with patch.dict(
            os.environ,
            {"LLM_DAILY_TOKEN_LIMIT": "invalid"},
            clear=False,
        ):
            response = self.client.get("/api/summary/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["llm_usage"]["blocked"])
        self.assertEqual(
            response.json()["llm_usage"]["configuration_error"],
            "invalid_budget",
        )


class QuizDomainSeedTests(TestCase):
    def test_default_quiz_domains_are_seeded(self):
        self.assertEqual(
            list(QuizDomainConfig.objects.values_list("slug", flat=True)),
            ["english", "japanese", "aws_saa"],
        )
