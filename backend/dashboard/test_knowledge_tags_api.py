import json
from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone

from .knowledge_tagging import publish_tag_snapshot
from .knowledge_tag_inventory import collect_knowledge_tag_inventory
from .views import knowledge_card_payload
from .models import (
    Category,
    ContentRun,
    CronJob,
    KnowledgeItem,
    KnowledgeTagActiveSnapshot,
    KnowledgeTagAssignment,
    SavedKnowledgeView,
)


NOW = datetime(2026, 7, 22, tzinfo=UTC)


def create_category_path(path: str) -> Category:
    parent = None
    pieces = []
    category = None
    for depth, name in enumerate(path.split("/"), start=1):
        pieces.append(name)
        category, _ = Category.objects.get_or_create(
            path="/".join(pieces),
            defaults={"name": name, "parent": parent, "depth": depth},
        )
        parent = category
    return category


class KnowledgeTagsApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = get_user_model().objects.create_user("owner", password="pw")
        self.ops = create_category_path("업무/운영/장애")
        self.other = create_category_path("업무/개발/API")
        self.english = create_category_path("학습/언어/영어")
        self.first = self.create_cron_item("first", self.ops, "alpha body", "a" * 64, 1)
        self.second = self.create_cron_item("second", self.other, "beta body", "b" * 64, 2)
        self.english_item = self.create_cron_item(
            "english",
            self.english,
            "english body",
            "c" * 64,
            3,
        )
        self.publish(
            {
                self.first.pk: ["보안", "Security", "AWS 보안"],
                self.second.pk: ["운영", "API", "장애"],
            }
        )

    def create_cron_item(
        self,
        external_id: str,
        category: Category,
        body: str,
        source_hash: str,
        hour: int,
    ) -> KnowledgeItem:
        job = CronJob.objects.create(external_id=external_id, name=external_id)
        run = ContentRun.objects.create(
            job=job,
            status=ContentRun.Status.SUCCESS,
            title=f"{external_id} title",
            body=body,
            generated_at=datetime(2026, 7, 22, hour, tzinfo=UTC),
        )
        return KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.CRON,
            source_key=f"cron:{run.pk}",
            content_run=run,
            category=category,
            status=KnowledgeItem.Status.CLASSIFIED,
            title=f"{external_id} title",
            summary=f"{external_id} summary",
            source_hash=source_hash,
            generated_at=datetime(2026, 7, 22, hour, tzinfo=UTC),
            classified_at=datetime(2026, 7, 22, hour, tzinfo=UTC),
        )

    def publish(self, reviewed: dict[int, list[str]]):
        inventory = collect_knowledge_tag_inventory()
        return publish_tag_snapshot(
            inventory_digest=inventory.inventory_digest,
            corpus_revision=inventory.corpus_revision,
            manifest={"version": "test"},
            reviewed=reviewed,
        )

    def patch_tags(self, item_id: int, tags):
        return self.client.patch(
            f"/api/knowledge/{item_id}/tags/",
            data=json.dumps({"tags": tags}),
            content_type="application/json",
        )

    def test_list_detail_search_filter_and_navigation_use_active_snapshot_tags(self):
        list_response = self.client.get("/api/knowledge/?tag=Security")
        search_response = self.client.get("/api/search/?q=AWS%20보안&tag=Security")
        detail_response = self.client.get(f"/api/knowledge/{self.first.pk}/")
        navigation_response = self.client.get(
            f"/api/knowledge/{self.first.pk}/navigation/?tag=Security"
        )

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()["canonical_query"], "tag=security")
        self.assertEqual(
            [item["id"] for item in list_response.json()["results"]],
            [self.first.pk],
        )
        self.assertEqual(
            list_response.json()["results"][0]["tags"],
            ["보안", "Security", "AWS 보안"],
        )
        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in search_response.json()["results"]],
            [self.first.pk],
        )
        self.assertEqual(detail_response.json()["tags"], ["보안", "Security", "AWS 보안"])
        self.assertEqual(navigation_response.status_code, 200)
        self.assertEqual(navigation_response.json()["canonical_query"], "tag=security")

    def test_list_request_uses_one_active_snapshot_for_filter_and_hydration(self):
        old_snapshot_id = KnowledgeTagActiveSnapshot.objects.get().snapshot_id
        new_snapshot = self.publish(
            {
                self.first.pk: ["신규", "교체", "태그"],
                self.second.pk: ["운영", "API", "장애"],
            }
        )
        pointer = KnowledgeTagActiveSnapshot.objects.get()
        KnowledgeTagActiveSnapshot.objects.filter(pk=pointer.pk).update(
            snapshot_id=old_snapshot_id,
            updated_at=timezone.now(),
        )

        original_attach = __import__(
            "dashboard.views",
            fromlist=["attach_tag_labels"],
        ).attach_tag_labels

        def swap_pointer_then_attach(items, *, snapshot_id=None):
            pointer = KnowledgeTagActiveSnapshot.objects.get()
            KnowledgeTagActiveSnapshot.objects.filter(pk=pointer.pk).update(
                snapshot=new_snapshot,
                updated_at=timezone.now(),
            )
            return original_attach(items, snapshot_id=snapshot_id)

        with patch(
            "dashboard.views.attach_tag_labels",
            side_effect=swap_pointer_then_attach,
        ):
            response = self.client.get("/api/knowledge/?tag=Security")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()["results"]], [self.first.pk])
        self.assertEqual(response.json()["results"][0]["tags"], ["보안", "Security", "AWS 보안"])

    def test_payload_builder_does_not_resolve_active_snapshot_implicitly(self):
        self.assertEqual(knowledge_card_payload(self.first)["tags"], [])

    def test_explicit_no_snapshot_does_not_reread_pointer_created_mid_request(self):
        snapshot_id = KnowledgeTagActiveSnapshot.objects.get().snapshot_id
        KnowledgeTagActiveSnapshot.objects.all().delete()
        original_attach = __import__(
            "dashboard.views",
            fromlist=["attach_tag_labels"],
        ).attach_tag_labels

        def create_pointer_then_attach(items, *, snapshot_id=None):
            KnowledgeTagActiveSnapshot.objects.get_or_create(
                singleton_key=1,
                defaults={"snapshot_id": snapshot_id_from_outer},
            )
            return original_attach(items, snapshot_id=snapshot_id)

        snapshot_id_from_outer = snapshot_id
        with patch(
            "dashboard.views.attach_tag_labels",
            side_effect=create_pointer_then_attach,
        ):
            response = self.client.get("/api/knowledge/")

        self.assertEqual(response.status_code, 200)
        first_payload = next(
            item for item in response.json()["results"] if item["id"] == self.first.pk
        )
        self.assertEqual(first_payload["tags"], [])

    def test_summary_free_question_and_run_detail_resolve_tags_once_per_response(self):
        pending = KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key="slack:tag:pending",
            status=KnowledgeItem.Status.PENDING,
            title="pending question",
            summary="pending summary",
            question="pending question",
            answer="pending answer",
            source_hash="f" * 64,
            generated_at=NOW,
        )
        self.publish(
            {
                self.first.pk: ["보안", "Security", "AWS 보안"],
                self.second.pk: ["운영", "API", "장애"],
                pending.pk: ["질문", "대기", "태그"],
            }
        )
        active_snapshot = KnowledgeTagActiveSnapshot.objects.get().snapshot_id
        calls = []

        def snapshot_id():
            calls.append("snapshot")
            return active_snapshot

        with patch("dashboard.views.active_tag_snapshot_id", side_effect=snapshot_id):
            summary_response = self.client.get("/api/summary/")
            free_response = self.client.get("/api/free-question/")
            run_response = self.client.get(f"/api/runs/{self.first.content_run_id}/")

        self.assertEqual(summary_response.status_code, 200)
        self.assertEqual(free_response.status_code, 200)
        self.assertEqual(run_response.status_code, 200)
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            free_response.json()["results"][0]["tags"],
            ["질문", "대기", "태그"],
        )

    def test_query_search_matches_tag_labels_with_other_text_fields_or(self):
        response = self.client.get("/api/knowledge/?q=API")

        self.assertEqual(response.status_code, 200)
        ids = [item["id"] for item in response.json()["results"]]
        self.assertIn(self.second.pk, ids)

    def test_saved_view_round_trips_tag_filter(self):
        created = self.client.post(
            "/api/saved-knowledge-views/",
            data=json.dumps({"name": "태그 보기", "filters": {"tag": " Security "}}),
            content_type="application/json",
        )
        applied = self.client.get(
            f"/api/saved-knowledge-views/{created.json()['id']}/apply/"
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["filters"], {"tag": "security"})
        self.assertEqual(applied.json()["canonical_query"], "tag=security")

    def test_replace_all_endpoint_is_owner_only_and_item_local(self):
        anonymous = self.patch_tags(self.first.pk, ["신규", "Security", "AWS 보안"])
        self.client.force_login(self.user)
        updated = self.patch_tags(self.first.pk, [" 신규 ", "Security", "AWS 보안"])

        self.assertEqual(anonymous.status_code, 403)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["tags"], ["신규", "Security", "AWS 보안"])
        active = KnowledgeTagActiveSnapshot.objects.get().snapshot
        self.assertEqual(
            list(
                KnowledgeTagAssignment.objects.filter(
                    snapshot=active,
                    knowledge_item=self.first,
                ).values_list("source", flat=True)
            ),
            ["user", "user", "user"],
        )
        self.assertEqual(
            list(
                KnowledgeTagAssignment.objects.filter(
                    snapshot=active,
                    knowledge_item=self.second,
                )
                .order_by("position")
                .values_list("tag__label", flat=True)
            ),
            ["운영", "API", "장애"],
        )

    def test_replace_validates_minimum_duplicates_control_hidden_and_learning(self):
        self.client.force_login(self.user)
        too_few = self.patch_tags(self.first.pk, ["a", "b"])
        duplicate = self.patch_tags(self.first.pk, ["a", "a", "b"])
        case_duplicate = self.patch_tags(self.first.pk, ["Security", "security", "b"])
        control = self.patch_tags(self.first.pk, ["a", "b", "bad\u0001"])
        hidden_item = self.create_cron_item("hidden", self.ops, "hidden", "e" * 64, 4)
        hidden_item.hidden_at = timezone.now()
        hidden_item.save(update_fields=["hidden_at", "updated_at"])
        hidden = self.patch_tags(hidden_item.pk, ["a", "b", "c"])
        learning = self.patch_tags(self.english_item.pk, ["a", "b", "c"])

        self.assertEqual(too_few.status_code, 400)
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(case_duplicate.status_code, 400)
        self.assertEqual(control.status_code, 400)
        self.assertEqual(hidden.status_code, 400)
        self.assertEqual(learning.status_code, 400)

    def test_manual_edit_before_publish_can_be_overwritten_after_pointer_swap(self):
        self.client.force_login(self.user)
        self.patch_tags(self.first.pk, ["수동", "Security", "AWS 보안"])
        new_snapshot = self.publish(
            {
                self.first.pk: ["야간", "재계산", "태그"],
                self.second.pk: ["운영", "API", "장애"],
            }
        )

        self.assertEqual(KnowledgeTagActiveSnapshot.objects.get().snapshot, new_snapshot)
        detail = self.client.get(f"/api/knowledge/{self.first.pk}/").json()
        self.assertEqual(detail["tags"], ["야간", "재계산", "태그"])

    def test_manual_edit_after_publish_targets_new_active_snapshot(self):
        self.client.force_login(self.user)
        new_snapshot = self.publish(
            {
                self.first.pk: ["야간", "재계산", "태그"],
                self.second.pk: ["운영", "API", "장애"],
            }
        )
        self.patch_tags(self.first.pk, ["수동", "Security", "AWS 보안"])

        self.assertEqual(
            list(
                KnowledgeTagAssignment.objects.filter(
                    snapshot=new_snapshot,
                    knowledge_item=self.first,
                )
                .order_by("position")
                .values_list("tag__label", flat=True)
            ),
            ["수동", "Security", "AWS 보안"],
        )
