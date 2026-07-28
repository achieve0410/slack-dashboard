import json
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from .data_lifecycle import (
    DataLifecycleError,
    backup_root,
    create_dashboard_backup,
    disconnect_slack_source,
    prune_dashboard_data,
)
from .models import ContentRun, CronJob, KnowledgeItem
from .services import reconcile_cron_runs


def create_slack_item(channel_id: str, *, age_days: int = 0) -> KnowledgeItem:
    job = CronJob.objects.create(
        external_id=f"channel:{channel_id}",
        name=f"#{channel_id.lower()}",
        channel_id=channel_id,
    )
    run = ContentRun.objects.create(
        job=job,
        external_ts=f"{job.pk}.100",
        status=ContentRun.Status.SUCCESS,
        title=f"{channel_id} 지식",
        body="Slack에서 가져온 지식입니다.",
        generated_at=timezone.now() - timedelta(days=age_days),
    )
    reconcile_cron_runs([run.pk])
    return KnowledgeItem.objects.get(content_run=run)


class SlackSourceLifecycleTests(TestCase):
    def test_disconnect_keeps_source_data_and_purge_is_scoped(self):
        target = create_slack_item("C1")
        other = create_slack_item("C2")

        disconnected = disconnect_slack_source("C1")

        self.assertEqual(disconnected.sources, 1)
        self.assertTrue(KnowledgeItem.objects.filter(pk=target.pk).exists())
        target.content_run.job.refresh_from_db()
        self.assertFalse(target.content_run.job.enabled)
        self.assertIsNotNone(target.content_run.job.disconnected_at)

        purged = disconnect_slack_source("C1", purge=True)

        self.assertEqual(purged.knowledge_items, 1)
        self.assertFalse(KnowledgeItem.objects.filter(pk=target.pk).exists())
        self.assertTrue(KnowledgeItem.objects.filter(pk=other.pk).exists())

    def test_purge_command_requires_explicit_confirmation(self):
        create_slack_item("C1")

        with self.assertRaisesRegex(CommandError, "--confirm"):
            call_command(
                "disconnect_slack_source",
                channel_id="C1",
                purge=True,
            )


class RetentionLifecycleTests(TestCase):
    def test_prune_hides_only_expired_slack_data_by_default(self):
        expired = create_slack_item("C1", age_days=40)
        current = create_slack_item("C2", age_days=3)

        result = prune_dashboard_data(days=30)

        self.assertEqual(result.knowledge_items, 1)
        expired.refresh_from_db()
        expired.content_run.refresh_from_db()
        current.refresh_from_db()
        self.assertIsNotNone(expired.hidden_at)
        self.assertIsNotNone(expired.content_run.hidden_at)
        self.assertIsNone(current.hidden_at)

    def test_hard_delete_removes_expired_source_records(self):
        expired = create_slack_item("C1", age_days=40)

        result = prune_dashboard_data(days=30, hard_delete=True)

        self.assertEqual(result.knowledge_items, 1)
        self.assertFalse(KnowledgeItem.objects.filter(pk=expired.pk).exists())
        self.assertFalse(
            ContentRun.objects.filter(pk=expired.content_run_id).exists()
        )

    def test_hard_delete_command_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(CommandError, "--confirm"):
            call_command(
                "prune_dashboard_data",
                days=30,
                hard_delete=True,
            )


class BackupLifecycleTests(TestCase):
    def test_backup_creates_valid_json_fixture_in_configured_directory(self):
        create_slack_item("C1")
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"DASHBOARD_BACKUP_DIR": directory},
        ):
            path = create_dashboard_backup(filename="dashboard-test.json")
            self.assertEqual(path.name, "dashboard-test.json")
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertTrue(
                any(row["model"] == "dashboard.knowledgeitem" for row in payload)
            )

    def test_backup_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"DASHBOARD_BACKUP_DIR": directory},
        ):
            with self.assertRaises(DataLifecycleError):
                create_dashboard_backup(filename="../outside.json")

    def test_blank_backup_directory_uses_project_default(self):
        with patch.dict(os.environ, {"DASHBOARD_BACKUP_DIR": ""}):
            self.assertEqual(
                backup_root().name,
                "backups",
            )
