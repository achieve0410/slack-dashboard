from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from .knowledge_actions import (
    KnowledgeActionError,
    apply_knowledge_action,
    canonical_action_parameters,
    normalize_target_ids,
    prune_bulk_snapshots,
    restore_items,
    target_digest,
    validate_snapshot_execution,
)
from .management.commands.sync_slack import Command as SyncCommand
from .models import (
    BulkSelectionSnapshot,
    Category,
    ContentRun,
    CronJob,
    KnowledgeConsumptionState,
    KnowledgeItem,
)
from .services import reconcile_cron_runs


NOW = datetime(2026, 7, 17, 3, tzinfo=UTC)


class SnapshotValidationTests(TestCase):
    def snapshot(self, **overrides):
        target_ids = overrides.pop("target_ids", [1, 2, 3])
        values = {
            "target_ids": target_ids,
            "target_count": len(target_ids),
            "target_digest": target_digest(target_ids),
            "action_type": "read",
            "action_parameters": {"value": True},
            "expires_at": NOW + timedelta(minutes=10),
            "consumed_at": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_direct_ids_normalize_dedupe_sort_and_limit(self):
        self.assertEqual(normalize_target_ids(["3", 1, "2", 1]), [1, 2, 3])
        with self.assertRaises(KnowledgeActionError):
            normalize_target_ids([])
        with self.assertRaises(KnowledgeActionError):
            normalize_target_ids([True])
        with self.assertRaises(KnowledgeActionError):
            normalize_target_ids(list(range(1, 1002)))

    def test_action_parameter_shapes_are_strict(self):
        self.assertEqual(
            canonical_action_parameters("completed", {"value": False}),
            {"value": False},
        )
        self.assertEqual(
            canonical_action_parameters("category", {"category_id": 7}),
            {"category_id": 7},
        )
        self.assertEqual(canonical_action_parameters("hide", {}), {})

        invalid = (
            ("read", {"value": 1}),
            ("category", {"category_id": "7"}),
            ("category", {"category_id": 7, "value": True}),
            ("hide", {"value": True}),
        )
        for action_type, parameters in invalid:
            with self.subTest(action_type=action_type), self.assertRaises(KnowledgeActionError):
                canonical_action_parameters(action_type, parameters)

    def test_same_count_membership_swap_is_rejected(self):
        with self.assertRaises(KnowledgeActionError) as raised:
            validate_snapshot_execution(
                self.snapshot(),
                [1, 2, 4],
                "read",
                {"value": True},
                now=NOW,
            )

        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.code, "snapshot_membership_changed")

    def test_action_parameter_swap_expiry_and_reuse_are_rejected(self):
        cases = (
            (
                self.snapshot(),
                "read",
                {"value": False},
                "snapshot_action_changed",
            ),
            (
                self.snapshot(expires_at=NOW),
                "read",
                {"value": True},
                "snapshot_expired",
            ),
            (
                self.snapshot(consumed_at=NOW - timedelta(seconds=1)),
                "read",
                {"value": True},
                "snapshot_consumed",
            ),
        )
        for snapshot, action_type, parameters, code in cases:
            with self.subTest(code=code), self.assertRaises(KnowledgeActionError) as raised:
                validate_snapshot_execution(
                    snapshot,
                    [1, 2, 3],
                    action_type,
                    parameters,
                    now=NOW,
                )
            self.assertEqual(raised.exception.code, code)


class BulkSnapshotRetentionTests(TestCase):
    @staticmethod
    def snapshot(label: str, *, expires_at, consumed_at=None):
        return BulkSelectionSnapshot.objects.create(
            token_hash=label.ljust(64, "0"),
            target_ids=[1],
            target_digest=target_digest([1]),
            target_count=1,
            action_type="read",
            action_parameters={"value": True},
            canonical_filter={},
            expires_at=expires_at,
            consumed_at=consumed_at,
        )

    def test_prune_bulk_snapshots_preserves_exact_retention_boundaries(self):
        expired_old = self.snapshot(
            "expired-old",
            expires_at=NOW - timedelta(hours=24, seconds=1),
        )
        expired_boundary = self.snapshot(
            "expired-boundary",
            expires_at=NOW - timedelta(hours=24),
        )
        consumed_old = self.snapshot(
            "consumed-old",
            expires_at=NOW - timedelta(days=8),
            consumed_at=NOW - timedelta(days=7, seconds=1),
        )
        consumed_boundary = self.snapshot(
            "consumed-boundary",
            expires_at=NOW - timedelta(days=8),
            consumed_at=NOW - timedelta(days=7),
        )

        self.assertEqual(prune_bulk_snapshots(now=NOW), 2)
        self.assertFalse(BulkSelectionSnapshot.objects.filter(pk=expired_old.pk).exists())
        self.assertFalse(BulkSelectionSnapshot.objects.filter(pk=consumed_old.pk).exists())
        self.assertTrue(BulkSelectionSnapshot.objects.filter(pk=expired_boundary.pk).exists())
        self.assertTrue(BulkSelectionSnapshot.objects.filter(pk=consumed_boundary.pk).exists())

    def test_sync_lifecycle_prunes_bulk_snapshots(self):
        expired_old = self.snapshot(
            "lifecycle-expired-old",
            expires_at=NOW - timedelta(hours=24, seconds=1),
        )
        command = SyncCommand()
        with (
            patch.object(command, "_sync", return_value={}),
            patch(
                "dashboard.management.commands.sync_slack.start_operation",
                return_value=object(),
            ),
            patch("dashboard.management.commands.sync_slack.finish_operation"),
            patch("dashboard.management.commands.sync_slack.prune_operation_runs"),
        ):
            command.handle()

        self.assertFalse(BulkSelectionSnapshot.objects.filter(pk=expired_old.pk).exists())

    def test_sync_lifecycle_isolates_snapshot_prune_failure(self):
        command = SyncCommand()
        with (
            patch.object(command, "_sync", return_value={}),
            patch(
                "dashboard.management.commands.sync_slack.start_operation",
                return_value=object(),
            ),
            patch("dashboard.management.commands.sync_slack.finish_operation"),
            patch("dashboard.management.commands.sync_slack.prune_operation_runs"),
            patch(
                "dashboard.management.commands.sync_slack.prune_bulk_snapshots",
                side_effect=RuntimeError("database unavailable"),
            ) as prune_snapshots,
            self.assertLogs(
                "dashboard.management.commands.sync_slack",
                level="WARNING",
            ) as logs,
        ):
            command.handle()

        prune_snapshots.assert_called_once_with()
        output = "\n".join(logs.output)
        self.assertIn("bulk_snapshot_prune_failed", output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("database unavailable", output)


class KnowledgeActionServiceTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="업무", path="업무", depth=1)
        self.pending = self.slack_item(
            "slack:bulk-thread:100.100",
            KnowledgeItem.Status.PENDING,
        )
        self.needs_review = self.slack_item(
            "slack:bulk-thread:100.200",
            KnowledgeItem.Status.NEEDS_REVIEW,
        )

    @staticmethod
    def slack_item(source_key: str, status: str, *, answer: str = "답변"):
        return KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key=source_key,
            status=status,
            title=source_key,
            summary="요약",
            question="질문",
            answer=answer,
            source_hash="a" * 64,
            generated_at=NOW,
        )

    def test_strict_classification_accepts_pending_and_needs_review(self):
        affected = apply_knowledge_action(
            [self.needs_review.pk, self.pending.pk],
            "category",
            {"category_id": self.category.pk},
            review_note="bulk 검토 완료",
            now=NOW,
        )

        self.assertEqual(affected, sorted([self.pending.pk, self.needs_review.pk]))
        for item in (self.pending, self.needs_review):
            item.refresh_from_db()
            self.assertEqual(item.status, KnowledgeItem.Status.CLASSIFIED)
            self.assertEqual(item.category, self.category)
            self.assertEqual(item.classification_reason, "bulk 검토 완료")

    def test_mixed_ineligible_classification_has_zero_writes(self):
        awaiting = self.slack_item(
            "slack:bulk-thread:100.300",
            KnowledgeItem.Status.AWAITING_ANSWER,
            answer="",
        )

        with self.assertRaises(KnowledgeActionError) as raised:
            apply_knowledge_action(
                [self.pending.pk, awaiting.pk],
                "category",
                {"category_id": self.category.pk},
                review_note="모두 검토",
                now=NOW,
            )

        self.assertEqual(raised.exception.status, 409)
        self.pending.refresh_from_db()
        awaiting.refresh_from_db()
        self.assertEqual(self.pending.status, KnowledgeItem.Status.PENDING)
        self.assertIsNone(self.pending.category_id)
        self.assertEqual(awaiting.status, KnowledgeItem.Status.AWAITING_ANSWER)

    def test_missing_review_note_is_400_with_zero_writes(self):
        with self.assertRaises(KnowledgeActionError) as raised:
            apply_knowledge_action(
                [self.pending.pk],
                "category",
                {"category_id": self.category.pk},
                review_note="  ",
                now=NOW,
            )

        self.assertEqual(raised.exception.status, 400)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, KnowledgeItem.Status.PENDING)

    def test_hide_restore_preserves_cron_source_and_consumption_state(self):
        job = CronJob.objects.create(external_id="bulk-cron", name="Bulk Cron")
        run = ContentRun.objects.create(
            job=job,
            status=ContentRun.Status.SUCCESS,
            title="Cron 항목",
            body="본문",
            generated_at=NOW,
        )
        reconcile_cron_runs([run.pk])
        item = KnowledgeItem.objects.get(content_run=run)
        state = KnowledgeConsumptionState.objects.create(
            knowledge_item=item,
            read_at=NOW,
            note="보존할 메모",
        )

        apply_knowledge_action([item.pk], "hide", {}, now=NOW)

        item.refresh_from_db()
        run.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(item.hidden_at, NOW)
        self.assertEqual(run.hidden_at, NOW)
        self.assertEqual(state.note, "보존할 메모")
        self.assertEqual(ContentRun.objects.count(), 1)

        restore_items([item.pk])
        restore_items([item.pk])
        item.refresh_from_db()
        run.refresh_from_db()
        state.refresh_from_db()
        self.assertIsNone(item.hidden_at)
        self.assertIsNone(run.hidden_at)
        self.assertEqual(state.read_at, NOW)
        self.assertEqual(state.note, "보존할 메모")

    def test_same_value_state_action_preserves_original_timestamp(self):
        apply_knowledge_action(
            [self.pending.pk],
            "read",
            {"value": True},
            now=NOW,
        )
        apply_knowledge_action(
            [self.pending.pk],
            "read",
            {"value": True},
            now=NOW + timedelta(minutes=5),
        )

        state = KnowledgeConsumptionState.objects.get(knowledge_item=self.pending)
        self.assertEqual(state.read_at, NOW)
