from datetime import UTC, datetime

from django.http import QueryDict
from django.test import Client, TestCase

from .knowledge_filters import (
    KnowledgeFilterError,
    apply_knowledge_filters,
    parse_knowledge_filters,
)
from .models import (
    Category,
    KnowledgeConsumptionState,
    KnowledgeItem,
    KnowledgeTag,
    KnowledgeTagActiveSnapshot,
    KnowledgeTagAssignment,
    KnowledgeTagSnapshot,
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



class KnowledgeFilterTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.category = create_category_path("학습/언어/영어")

    def create_item(self, suffix: str, hour: int) -> KnowledgeItem:
        return KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key=f"slack:filters:{suffix}",
            category=self.category,
            status=KnowledgeItem.Status.CLASSIFIED,
            title=f"필터 제목 {suffix}",
            summary=f"필터 요약 {suffix}",
            question=f"필터 질문 {suffix}",
            answer=f"필터 답변 {suffix}",
            source_hash=suffix[0] * 64,
            generated_at=datetime(2026, 7, 16, hour, tzinfo=UTC),
            classified_at=datetime(2026, 7, 16, hour, tzinfo=UTC),
        )

    def test_parser_uses_canonical_key_order_and_rejects_unknown_or_repeated_keys(self):
        parsed = parse_knowledge_filters(
            QueryDict(
                f"sort=oldest&category={self.category.pk}&q=%20filter%20"
                "&archived=include&completed=1"
            )
        )

        self.assertEqual(
            list(parsed.values),
            ["q", "category", "completed", "archived", "sort"],
        )
        self.assertEqual(
            parsed.canonical_query,
            f"q=filter&category={self.category.pk}&completed=1"
            "&archived=include&sort=oldest",
        )
        with self.assertRaises(KnowledgeFilterError):
            parse_knowledge_filters(QueryDict("context={}&sort=newest"))
        with self.assertRaises(KnowledgeFilterError):
            parse_knowledge_filters(QueryDict("status=pending&status=classified"))

    def test_parser_validates_custom_dates_and_search_requires_query(self):
        for query in (
            "period=custom&from=2026-07-18&to=2026-07-17",
            "from=2026-07-17",
            "period=custom&from=bad&to=2026-07-17",
        ):
            with self.subTest(query=query), self.assertRaises(KnowledgeFilterError):
                parse_knowledge_filters(QueryDict(query))
        with self.assertRaises(KnowledgeFilterError) as error:
            parse_knowledge_filters(QueryDict("sort=oldest"), required_query=True)
        self.assertEqual(error.exception.code, "query_required")

    def test_filters_apply_before_pagination_and_preserve_completed_archive_independence(self):
        first = self.create_item("first", 1)
        second = self.create_item("second", 2)
        archived = self.create_item("archived", 3)
        KnowledgeConsumptionState.objects.create(
            knowledge_item=first,
            read_at=datetime(2026, 7, 16, 4, tzinfo=UTC),
            bookmarked_at=datetime(2026, 7, 16, 4, tzinfo=UTC),
            completed_at=datetime(2026, 7, 16, 4, tzinfo=UTC),
        )
        KnowledgeConsumptionState.objects.create(
            knowledge_item=second,
            read_at=datetime(2026, 7, 16, 5, tzinfo=UTC),
            bookmarked_at=datetime(2026, 7, 16, 5, tzinfo=UTC),
            completed_at=datetime(2026, 7, 16, 5, tzinfo=UTC),
        )
        KnowledgeConsumptionState.objects.create(
            knowledge_item=archived,
            completed_at=datetime(2026, 7, 16, 6, tzinfo=UTC),
            archived_at=datetime(2026, 7, 16, 6, tzinfo=UTC),
        )

        response = self.client.get(
            "/api/knowledge/",
            {
                "source_type": "slack_qa",
                "read": "read",
                "bookmarked": "1",
                "completed": "1",
                "period": "custom",
                "from": "2026-07-16",
                "to": "2026-07-16",
                "sort": "oldest",
                "limit": 1,
                "offset": 1,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 2)
        self.assertEqual(
            [item["id"] for item in response.json()["results"]],
            [second.pk],
        )
        default_ids = {
            item["id"] for item in self.client.get("/api/knowledge/").json()["results"]
        }
        completed_ids = {
            item["id"]
            for item in self.client.get("/api/knowledge/?completed=1").json()["results"]
        }
        archived_ids = {
            item["id"]
            for item in self.client.get("/api/knowledge/?archived=only").json()["results"]
        }
        self.assertIn(first.pk, default_ids)
        self.assertIn(first.pk, completed_ids)
        self.assertNotIn(archived.pk, default_ids)
        self.assertEqual(archived_ids, {archived.pk})

    def test_list_and_search_share_filters_and_primary_key_tie_breaker(self):
        first = self.create_item("shared-first", 7)
        second = self.create_item("shared-second", 7)
        KnowledgeConsumptionState.objects.create(
            knowledge_item=second,
            read_at=datetime(2026, 7, 16, 8, tzinfo=UTC),
        )

        list_response = self.client.get("/api/knowledge/?q=shared&read=read")
        search_response = self.client.get("/api/search/?q=shared&read=read")

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in list_response.json()["results"]],
            [second.pk],
        )
        self.assertEqual(
            [item["id"] for item in search_response.json()["results"]],
            [second.pk],
        )
        unfiltered = self.client.get("/api/knowledge/?q=shared").json()["results"]
        self.assertEqual([item["id"] for item in unfiltered], [second.pk, first.pk])

    def test_apply_q_and_tag_uses_one_explicit_snapshot_id(self):
        first = self.create_item("tagged", 9)
        second = self.create_item("other", 10)
        first_snapshot = self.create_tag_snapshot(first)
        second_snapshot = self.create_tag_snapshot(second)
        KnowledgeTagActiveSnapshot.objects.update(snapshot=second_snapshot)
        parsed = parse_knowledge_filters(QueryDict("q=Security&tag=Security"))

        queryset = apply_knowledge_filters(
            KnowledgeItem.objects.all(),
            parsed,
            tag_snapshot_id=first_snapshot.pk,
        )

        self.assertEqual(list(queryset.values_list("pk", flat=True)), [first.pk])
        self.assertEqual(KnowledgeTagActiveSnapshot.objects.get().snapshot, second_snapshot)

    def test_apply_explicit_no_snapshot_does_not_reread_new_pointer(self):
        item = self.create_item("new-pointer", 11)
        KnowledgeTagActiveSnapshot.objects.all().delete()
        parsed = parse_knowledge_filters(QueryDict("tag=Security"))
        snapshot = self.create_tag_snapshot(item)
        KnowledgeTagActiveSnapshot.objects.create(snapshot=snapshot)

        queryset = apply_knowledge_filters(
            KnowledgeItem.objects.all(),
            parsed,
            tag_snapshot_id=None,
        )

        self.assertEqual(list(queryset.values_list("pk", flat=True)), [])

    def create_tag_snapshot(self, item: KnowledgeItem) -> KnowledgeTagSnapshot:
        snapshot = KnowledgeTagSnapshot.objects.create(
            status=KnowledgeTagSnapshot.Status.ACTIVE,
            inventory_digest="d" * 64,
            artifact_manifest={"version": "test"},
            item_count=1,
            tag_count=3,
            assignment_count=3,
        )
        for position, label in enumerate(["Security", "보안", "AWS 보안"], start=1):
            KnowledgeTagAssignment.objects.create(
                snapshot=snapshot,
                knowledge_item=item,
                tag=KnowledgeTag.for_label(label),
                position=position,
            )
        return snapshot
