from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from .models import OperationRun
from .operation_runs import (
    OperationAttempt,
    finish_operation,
    freshness_state,
    prune_operation_runs,
    start_operation,
    validate_operation_details,
)


SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 7, 17, 12, tzinfo=SEOUL)


class RaisingManager:
    def create(self, **_kwargs):
        raise RuntimeError("raw title secret-token")

    def filter(self, *_args, **_kwargs):
        raise RuntimeError("raw title secret-token")


class RaisingModel:
    objects = RaisingManager()


class SuccessfulManager:
    def __init__(self):
        self.created = None
        self.updated = None

    def create(self, **kwargs):
        self.created = kwargs
        return SimpleNamespace(pk=17)

    def filter(self, **kwargs):
        self.filter_kwargs = kwargs
        return self

    def update(self, **kwargs):
        self.updated = kwargs
        return 1


class OperationRunTests(SimpleTestCase):
    def test_allowlists_and_summary_size_fail_closed(self):
        validate_operation_details(
            "classify",
            "success",
            "",
            {"selected": 2, "classified": 1, "needs_review": 1},
        )
        validate_operation_details(
            "quiz",
            "success",
            "",
            {
                "quiz_candidates": 3,
                "quiz_published": 2,
                "quiz_quarantined": 1,
                "quiz_failed": 0,
                "quiz_dry_run": False,
            },
        )
        validate_operation_details(
            "tagging",
            "success",
            "tag_stale_inventory",
            {
                "tag_inventory": 5,
                "tag_candidates": 12,
                "tag_assigned_items": 5,
                "tag_assignments": 19,
                "tag_reviewed_items": 5,
                "tag_failed_items": 0,
                "tag_dry_run": True,
                "tag_published": False,
                "tag_stale_inventory": True,
            },
        )

        invalid = (
            ("other", "success", "", {}),
            ("sync", "other", "", {}),
            ("sync", "failed", "raw_error", {}),
            ("sync", "success", "", {"title": 1}),
            ("quiz", "success", "", {"candidate_outcomes": 1}),
            ("tagging", "failed", "tag_raw_error", {}),
            ("tagging", "success", "", {"tag_label": 1}),
            ("sync", "success", "", {"selected": "raw text"}),
        )
        for details in invalid:
            with self.subTest(details=details), self.assertRaises(ValidationError):
                validate_operation_details(*details)

        with self.assertRaises(ValidationError):
            validate_operation_details(
                "sync",
                "success",
                "",
                {"selected": 10**4199},
            )

    def test_sync_and_classify_staleness_use_strict_boundaries(self):
        for elapsed, expected in (
            (timedelta(minutes=29, seconds=59), False),
            (timedelta(minutes=30), False),
            (timedelta(minutes=30, seconds=1), True),
        ):
            with self.subTest(kind="sync", elapsed=elapsed):
                state = freshness_state("sync", NOW - elapsed, now=NOW)
                self.assertEqual(state["stale"], expected)

        for elapsed, expected in (
            (timedelta(hours=35, minutes=59), False),
            (timedelta(hours=36), False),
            (timedelta(hours=36, seconds=1), True),
        ):
            with self.subTest(kind="classify", elapsed=elapsed):
                state = freshness_state("classify", NOW - elapsed, now=NOW)
                self.assertEqual(state["stale"], expected)

        self.assertTrue(freshness_state("sync", None, now=NOW)["stale"])

        for elapsed, expected in (
            (timedelta(days=6, hours=23, minutes=59), False),
            (timedelta(days=7), False),
            (timedelta(days=7, seconds=1), True),
        ):
            with self.subTest(kind="quiz", elapsed=elapsed):
                state = freshness_state("quiz", NOW - elapsed, now=NOW)
                self.assertEqual(state["stale"], expected)

        for elapsed, expected in (
            (timedelta(hours=35, minutes=59), False),
            (timedelta(hours=36), False),
            (timedelta(hours=36, seconds=1), True),
        ):
            with self.subTest(kind="tagging", elapsed=elapsed):
                state = freshness_state("tagging", NOW - elapsed, now=NOW)
                self.assertEqual(state["stale"], expected)

    def test_logging_database_failure_does_not_escape_or_leak_raw_error(self):
        with self.assertLogs("dashboard.operation_runs", level="WARNING") as logs:
            attempt = start_operation("sync", model=RaisingModel, now=NOW)
            finished = finish_operation(
                attempt,
                "failed",
                error_code="database_error",
                model=RaisingModel,
                now=NOW,
            )

        self.assertIsNone(attempt.run_id)
        self.assertFalse(finished)
        output = "\n".join(logs.output)
        self.assertIn("operation_log_fallback", output)
        self.assertNotIn("raw title", output)
        self.assertNotIn("secret-token", output)

    def test_prune_database_failure_is_isolated_without_leaking_raw_error(self):
        with self.assertLogs("dashboard.operation_runs", level="WARNING") as logs:
            deleted = prune_operation_runs(model=RaisingModel, now=NOW)

        self.assertEqual(deleted, 0)
        output = "\n".join(logs.output)
        self.assertIn("operation_log_fallback", output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("raw title", output)
        self.assertNotIn("secret-token", output)

    def test_successful_start_and_finish_persist_allowlisted_fields(self):
        manager = SuccessfulManager()
        model = SimpleNamespace(objects=manager)

        attempt = start_operation("classify", model=model, now=NOW)
        result = finish_operation(
            attempt,
            "success",
            summary={"selected": 2, "classified": 2},
            model=model,
            now=NOW + timedelta(seconds=4),
        )

        self.assertEqual(attempt, OperationAttempt("classify", NOW, 17))
        self.assertTrue(result)
        self.assertEqual(manager.created["status"], "running")
        self.assertEqual(manager.filter_kwargs, {"pk": 17})
        self.assertEqual(manager.updated["status"], "success")
        self.assertEqual(manager.updated["summary"], {"selected": 2, "classified": 2})

    def test_quiz_operation_persists_only_aggregate_summary(self):
        manager = SuccessfulManager()
        model = SimpleNamespace(objects=manager)

        attempt = start_operation("quiz", model=model, now=NOW)
        result = finish_operation(
            attempt,
            "success",
            summary={
                "quiz_candidates": 4,
                "quiz_published": 3,
                "quiz_quarantined": 1,
                "quiz_failed": 0,
                "quiz_dry_run": True,
            },
            model=model,
            now=NOW + timedelta(seconds=8),
        )

        self.assertTrue(result)
        self.assertEqual(manager.created["kind"], "quiz")
        self.assertEqual(manager.updated["summary"]["quiz_quarantined"], 1)

    def test_tagging_operation_persists_only_aggregate_summary(self):
        manager = SuccessfulManager()
        model = SimpleNamespace(objects=manager)

        attempt = start_operation("tagging", model=model, now=NOW)
        result = finish_operation(
            attempt,
            "success",
            summary={
                "tag_inventory": 4,
                "tag_candidates": 11,
                "tag_assigned_items": 4,
                "tag_assignments": 13,
                "tag_reviewed_items": 4,
                "tag_failed_items": 0,
                "tag_dry_run": False,
                "tag_published": True,
                "tag_stale_inventory": False,
            },
            model=model,
            now=NOW + timedelta(seconds=8),
        )

        self.assertTrue(result)
        self.assertEqual(manager.created["kind"], "tagging")
        self.assertEqual(manager.updated["summary"]["tag_assignments"], 13)


class OperationRunRetentionTests(TestCase):
    @staticmethod
    def create_run(*, started_at, finished_at=None):
        return OperationRun.objects.create(
            kind="sync",
            status="running" if finished_at is None else "success",
            error_code="",
            summary={},
            started_at=started_at,
            finished_at=finished_at,
        )

    def test_prune_operation_runs_prunes_old_unfinished_and_preserves_boundary(self):
        old_unfinished = self.create_run(started_at=NOW - timedelta(days=90, seconds=1))
        boundary_unfinished = self.create_run(started_at=NOW - timedelta(days=90))
        old_finished = self.create_run(
            started_at=NOW - timedelta(days=91),
            finished_at=NOW - timedelta(days=90, seconds=1),
        )
        boundary_finished = self.create_run(
            started_at=NOW - timedelta(days=91),
            finished_at=NOW - timedelta(days=90),
        )

        self.assertEqual(prune_operation_runs(now=NOW), 2)
        self.assertFalse(OperationRun.objects.filter(pk=old_unfinished.pk).exists())
        self.assertFalse(OperationRun.objects.filter(pk=old_finished.pk).exists())
        self.assertTrue(OperationRun.objects.filter(pk=boundary_unfinished.pk).exists())
        self.assertTrue(OperationRun.objects.filter(pk=boundary_finished.pk).exists())
