import json
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.test import Client, TestCase

from . import knowledge_actions
from .knowledge_actions import token_hash
from .models import (
    BulkSelectionSnapshot,
    Category,
    KnowledgeConsumptionState,
    KnowledgeItem,
    OperationRun,
    ScheduleCategory,
    ScheduleEvent,
)

def create_category_path(path: str) -> Category:
    parent = None
    category = None
    accumulated = []
    for depth, name in enumerate(path.split("/"), start=1):
        accumulated.append(name)
        full_path = "/".join(accumulated)
        category, _ = Category.objects.get_or_create(
            path=full_path,
            defaults={"name": name, "parent": parent, "depth": depth, "is_active": True},
        )
        parent = category
    return category



SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 7, 17, 12, tzinfo=SEOUL)


class IntegratedBackendApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.category = create_category_path("학습/언어/영어")
        self.todo_category = ScheduleCategory.objects.get(is_fallback=True)

    def post(self, url: str, data):
        return self.client.post(
            url,
            data=json.dumps(data),
            content_type="application/json",
        )

    def create_item(
        self,
        suffix: str,
        *,
        status: str = KnowledgeItem.Status.CLASSIFIED,
    ) -> KnowledgeItem:
        classified = status == KnowledgeItem.Status.CLASSIFIED
        return KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key=f"slack:integrated:{suffix}",
            category=self.category if classified else None,
            status=status,
            title=f"통합 테스트 {suffix}",
            summary=f"요약 {suffix}",
            question=f"질문 {suffix}",
            answer=f"답변 {suffix}",
            source_hash="a" * 64,
            generated_at=datetime(2026, 7, 17, tzinfo=UTC),
            classified_at=datetime(2026, 7, 17, tzinfo=UTC) if classified else None,
        )

    def execute_after_lock_window_mutation(self, token: str, mutation):
        original_lock = knowledge_actions._lock_visible_items

        def mutate_then_lock(item_ids):
            mutation()
            return original_lock(item_ids)

        with patch(
            "dashboard.knowledge_actions._lock_visible_items",
            side_effect=mutate_then_lock,
        ):
            return self.post(
                "/api/knowledge/bulk/execute/",
                {
                    "token": token,
                    "action": "bookmarked",
                    "parameters": {"value": True},
                },
            )

    def test_bulk_preview_execute_is_single_use_and_never_persists_raw_token(self):
        item = self.create_item("state")
        preview = self.post(
            "/api/knowledge/bulk/preview/",
            {
                "ids": [str(item.pk), item.pk],
                "action": "read",
                "parameters": {"value": True},
            },
        )

        self.assertEqual(preview.status_code, 201)
        token = preview.json()["token"]
        snapshot = BulkSelectionSnapshot.objects.get()
        self.assertEqual(snapshot.token_hash, token_hash(token))
        self.assertNotEqual(snapshot.token_hash, token)
        self.assertEqual(snapshot.target_ids, [item.pk])

        executed = self.post(
            "/api/knowledge/bulk/execute/",
            {"token": token, "action": "read", "parameters": {"value": True}},
        )
        repeated = self.post(
            "/api/knowledge/bulk/execute/",
            {"token": token, "action": "read", "parameters": {"value": True}},
        )

        self.assertEqual(executed.status_code, 200)
        self.assertEqual(executed.json()["affected_ids"], [item.pk])
        self.assertTrue(
            KnowledgeConsumptionState.objects.get(knowledge_item=item).read_at
        )
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(repeated.json()["code"], "snapshot_consumed")

    def test_filter_snapshot_status_drift_before_lock_is_409_with_zero_writes(self):
        item = self.create_item("status-race")
        preview = self.post(
            "/api/knowledge/bulk/preview/",
            {
                "filter": {"status": KnowledgeItem.Status.CLASSIFIED},
                "action": "bookmarked",
                "parameters": {"value": True},
            },
        )

        response = self.execute_after_lock_window_mutation(
            preview.json()["token"],
            lambda: KnowledgeItem.objects.filter(pk=item.pk).update(
                status=KnowledgeItem.Status.PENDING,
                category=None,
                classified_at=None,
            ),
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "snapshot_membership_changed")
        item.refresh_from_db()
        self.assertEqual(item.status, KnowledgeItem.Status.CLASSIFIED)
        self.assertFalse(
            KnowledgeConsumptionState.objects.filter(knowledge_item=item).exists()
        )
        snapshot = BulkSelectionSnapshot.objects.get()
        self.assertIsNone(snapshot.consumed_at)
        self.assertEqual(snapshot.affected_ids, [])

    def test_filter_snapshot_category_drift_before_lock_is_409_with_zero_writes(self):
        item = self.create_item("category-race")
        other_category = Category.objects.create(
            name="다른 카테고리",
            path="다른 카테고리",
            depth=1,
        )
        preview = self.post(
            "/api/knowledge/bulk/preview/",
            {
                "filter": {"category": self.category.pk},
                "action": "bookmarked",
                "parameters": {"value": True},
            },
        )

        response = self.execute_after_lock_window_mutation(
            preview.json()["token"],
            lambda: KnowledgeItem.objects.filter(pk=item.pk).update(
                category=other_category
            ),
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "snapshot_membership_changed")
        item.refresh_from_db()
        self.assertEqual(item.category, self.category)
        self.assertFalse(
            KnowledgeConsumptionState.objects.filter(knowledge_item=item).exists()
        )
        snapshot = BulkSelectionSnapshot.objects.get()
        self.assertIsNone(snapshot.consumed_at)
        self.assertEqual(snapshot.affected_ids, [])

    def test_filter_snapshot_state_drift_before_lock_is_409_with_zero_writes(self):
        item = self.create_item("state-race")
        preview = self.post(
            "/api/knowledge/bulk/preview/",
            {
                "filter": {"read": "unread"},
                "action": "bookmarked",
                "parameters": {"value": True},
            },
        )

        response = self.execute_after_lock_window_mutation(
            preview.json()["token"],
            lambda: KnowledgeConsumptionState.objects.create(
                knowledge_item=item,
                read_at=NOW,
            ),
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "snapshot_membership_changed")
        self.assertFalse(
            KnowledgeConsumptionState.objects.filter(knowledge_item=item).exists()
        )
        snapshot = BulkSelectionSnapshot.objects.get()
        self.assertIsNone(snapshot.consumed_at)
        self.assertEqual(snapshot.affected_ids, [])

    def test_explicit_id_snapshot_ignores_non_visibility_drift_before_lock(self):
        item = self.create_item("explicit-id-race")
        preview = self.post(
            "/api/knowledge/bulk/preview/",
            {
                "ids": [item.pk],
                "action": "bookmarked",
                "parameters": {"value": True},
            },
        )

        response = self.execute_after_lock_window_mutation(
            preview.json()["token"],
            lambda: KnowledgeItem.objects.filter(pk=item.pk).update(
                status=KnowledgeItem.Status.PENDING,
                category=None,
                classified_at=None,
            ),
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.status, KnowledgeItem.Status.PENDING)
        self.assertIsNotNone(
            KnowledgeConsumptionState.objects.get(knowledge_item=item).bookmarked_at
        )

    def test_strict_category_and_hide_trash_restore_undo_contracts(self):
        pending = self.create_item("pending", status=KnowledgeItem.Status.PENDING)
        category_preview = self.post(
            "/api/knowledge/bulk/preview/",
            {
                "ids": [pending.pk],
                "action": "category",
                "parameters": {"category_id": self.category.pk},
            },
        )
        rejected = self.post(
            "/api/knowledge/bulk/execute/",
            {
                "token": category_preview.json()["token"],
                "action": "category",
                "parameters": {"category_id": self.category.pk},
            },
        )
        pending.refresh_from_db()
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()["code"], "invalid_review_note")
        self.assertEqual(pending.status, KnowledgeItem.Status.PENDING)

        item = self.create_item("hide")
        hide_preview = self.post(
            "/api/knowledge/bulk/preview/",
            {"ids": [item.pk], "action": "hide", "parameters": {}},
        )
        hidden = self.post(
            "/api/knowledge/bulk/execute/",
            {
                "token": hide_preview.json()["token"],
                "action": "hide",
                "parameters": {},
            },
        )
        trash = self.client.get("/api/knowledge/trash/")
        restored = self.post(f"/api/knowledge/{item.pk}/restore/", {})
        restored_again = self.post(f"/api/knowledge/{item.pk}/restore/", {})
        undone = self.post(
            "/api/knowledge/bulk/undo/",
            {"token": hide_preview.json()["token"]},
        )

        self.assertEqual(hidden.status_code, 200)
        self.assertEqual(trash.status_code, 200)
        self.assertEqual(trash.json()["results"][0]["id"], item.pk)
        self.assertEqual(restored.status_code, 204)
        self.assertEqual(restored_again.status_code, 204)
        self.assertEqual(undone.status_code, 200)

    @patch("dashboard.schedule_groups.timezone.now", return_value=NOW)
    def test_grouped_schedule_preserves_flat_results_and_exhaustive_counts(self, _now):
        fixtures = (
            ScheduleEvent(
                title="완료",
                item_type=ScheduleEvent.ItemType.SCHEDULE,
                starts_at=datetime(2026, 7, 16, tzinfo=SEOUL),
                completed=True,
            ),
            ScheduleEvent(
                title="오늘",
                item_type=ScheduleEvent.ItemType.SCHEDULE,
                starts_at=datetime(2026, 7, 17, 9, tzinfo=SEOUL),
            ),
            ScheduleEvent(
                title="지연",
                item_type=ScheduleEvent.ItemType.TODO,
                todo_category=self.todo_category,
                starts_at=datetime(2026, 7, 16, tzinfo=SEOUL),
            ),
            ScheduleEvent(
                title="지난 일정",
                item_type=ScheduleEvent.ItemType.SCHEDULE,
                starts_at=datetime(2026, 7, 16, tzinfo=SEOUL),
            ),
            ScheduleEvent(
                title="예정",
                item_type=ScheduleEvent.ItemType.SCHEDULE,
                starts_at=datetime(2026, 7, 18, tzinfo=SEOUL),
            ),
            ScheduleEvent(
                title="기한 없음",
                item_type=ScheduleEvent.ItemType.TODO,
                todo_category=self.todo_category,
                starts_at=None,
            ),
        )
        ScheduleEvent.objects.bulk_create(fixtures)

        flat = self.client.get("/api/schedule/").json()
        grouped = self.client.get("/api/schedule/?grouped=1").json()

        self.assertEqual(grouped["count"], flat["count"])
        self.assertEqual(
            [event["id"] for event in grouped["results"]],
            [event["id"] for event in flat["results"]],
        )
        self.assertTrue(all("agenda_group" in event for event in grouped["results"]))
        self.assertTrue(all("agenda_group" not in event for event in flat["results"]))
        self.assertEqual(sum(grouped["group_counts"].values()), grouped["count"])
        self.assertEqual(set(grouped["group_counts"].values()), {1})

    def test_grouped_schedule_returns_all_results_beyond_flat_limit(self):
        ScheduleEvent.objects.bulk_create(
            [
                ScheduleEvent(
                    title=f"대량 할 일 {index}",
                    item_type=ScheduleEvent.ItemType.TODO,
                    todo_category=self.todo_category,
                )
                for index in range(205)
            ]
        )

        flat = self.client.get("/api/schedule/").json()
        grouped = self.client.get("/api/schedule/?grouped=1").json()

        self.assertEqual(flat["count"], 205)
        self.assertEqual(len(flat["results"]), 200)
        self.assertEqual(grouped["count"], 205)
        self.assertEqual(len(grouped["results"]), 205)
        self.assertEqual(sum(grouped["group_counts"].values()), 205)
        self.assertTrue(
            all(event["agenda_group"] == "undated" for event in grouped["results"])
        )

    def test_operations_api_and_command_boundaries_persist_status_without_changing_results(self):
        OperationRun.objects.create(
            kind=OperationRun.Kind.CLASSIFY,
            status=OperationRun.Status.SUCCESS,
            error_code="",
            summary={"selected": 1, "classified": 1},
            started_at=NOW,
            finished_at=NOW,
        )
        OperationRun.objects.create(
            kind=OperationRun.Kind.TAGGING,
            status=OperationRun.Status.SUCCESS,
            error_code="",
            summary={"tag_inventory": 1, "tag_published": True},
            started_at=NOW,
            finished_at=NOW,
        )
        operations = self.client.get("/api/operations/?limit=20")
        summary = self.client.get("/api/summary/")
        self.assertEqual(operations.status_code, 200)
        self.assertIn("classify", operations.json()["operations"])
        self.assertIn("tagging", operations.json()["operations"])
        self.assertEqual(
            operations.json()["operations"]["tagging"]["schedule_label"],
            "매일 02:00",
        )
        self.assertIn("total", operations.json()["backlog"])
        self.assertIn("operations", summary.json())
        self.assertIn("tagging", summary.json()["operations"])
        self.assertIn("backlog", summary.json())

        with patch(
            "slack_sdk.WebClient.auth_test",
            return_value={"ok": True, "user_id": "B1"},
        ):
            call_command("sync_slack", slack_token="test-token")
        self.assertEqual(
            OperationRun.objects.filter(kind="sync", status="success").count(),
            1,
        )

        @contextmanager
        def lock_contended(_path):
            yield False

        with patch(
            "dashboard.management.commands.classify_knowledge.classifier_lock",
            lock_contended,
        ):
            call_command("classify_knowledge")
        self.assertEqual(
            OperationRun.objects.filter(
                kind="classify",
                status="skipped",
                error_code="lock_contended",
            ).count(),
            1,
        )
