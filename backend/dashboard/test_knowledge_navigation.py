from datetime import UTC, datetime

from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .models import Category, KnowledgeConsumptionState, KnowledgeItem

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



class KnowledgeNavigationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.category = create_category_path("학습/언어/영어")

    def create_item(
        self,
        suffix: str,
        hour: int,
        *,
        category: Category | None = None,
    ) -> KnowledgeItem:
        category = category or self.category
        return KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key=f"slack:navigation:{suffix}",
            category=category,
            status=KnowledgeItem.Status.CLASSIFIED,
            title=f"공유 토큰 {suffix}",
            summary=f"요약 {suffix}",
            question=f"질문 {suffix}",
            answer=f"답변 {suffix}",
            source_hash=(suffix[0] if suffix else "x") * 64,
            generated_at=datetime(2026, 7, 16, hour, tzinfo=UTC),
            classified_at=datetime(2026, 7, 16, hour, tzinfo=UTC),
        )

    def test_navigation_matches_list_sequence_with_stable_tie_breaker(self):
        oldest = self.create_item("oldest", 1)
        middle = self.create_item("middle", 2)
        newest = self.create_item("newest", 2)
        query = f"status=classified&category={self.category.pk}&sort=newest"
        listed_ids = [
            item["id"]
            for item in self.client.get(f"/api/knowledge/?{query}").json()["results"]
        ]

        response = self.client.get(
            f"/api/knowledge/{middle.pk}/navigation/?{query}"
        )

        self.assertEqual(listed_ids, [newest.pk, middle.pk, oldest.pk])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["previous"]["id"], newest.pk)
        self.assertEqual(response.json()["next"]["id"], oldest.pk)
        self.assertEqual(response.json()["position"], 2)
        self.assertEqual(response.json()["total"], 3)

    def test_direct_context_and_context_changed_contract(self):
        item = self.create_item("context", 3)

        direct = self.client.get(f"/api/knowledge/{item.pk}/navigation/")
        item.hidden_at = timezone.now()
        item.save(update_fields=["hidden_at", "updated_at"])
        changed = self.client.get(f"/api/knowledge/{item.pk}/navigation/")
        invalid = self.client.get(
            f"/api/knowledge/{item.pk}/navigation/?context={{}}"
        )

        self.assertEqual(direct.status_code, 200)
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json()["code"], "context_changed")
        self.assertEqual(invalid.status_code, 400)

    def test_related_is_bounded_and_excludes_hidden_archived_and_other_categories(self):
        current = self.create_item("current", 1)
        candidates = [self.create_item(f"candidate-{index}", 2) for index in range(55)]
        hidden = candidates[0]
        hidden.hidden_at = timezone.now()
        hidden.save(update_fields=["hidden_at", "updated_at"])
        archived = candidates[1]
        KnowledgeConsumptionState.objects.create(
            knowledge_item=archived,
            archived_at=timezone.now(),
        )
        other_category = create_category_path("학습/언어/일본어")
        other = self.create_item("other", 3, category=other_category)

        related = self.client.get(
            f"/api/knowledge/{current.pk}/navigation/"
        ).json()["related"]
        related_ids = {item["id"] for item in related}

        self.assertEqual(len(related), 5)
        self.assertNotIn(current.pk, related_ids)
        self.assertNotIn(hidden.pk, related_ids)
        self.assertNotIn(archived.pk, related_ids)
        self.assertNotIn(other.pk, related_ids)

    def test_navigation_query_count_does_not_depend_on_page_size(self):
        items = [self.create_item(f"query-{index}", index) for index in range(1, 6)]
        url = f"/api/knowledge/{items[2].pk}/navigation/"
        self.client.get(url)

        with CaptureQueriesContext(connection) as small:
            small_response = self.client.get(f"{url}?limit=1")
        with CaptureQueriesContext(connection) as large:
            large_response = self.client.get(f"{url}?limit=100")

        self.assertEqual(small_response.status_code, 200)
        self.assertEqual(large_response.status_code, 200)
        self.assertEqual(len(small), len(large))
