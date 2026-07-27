import json

from django.db import IntegrityError, transaction
from django.test import Client, TestCase

from .models import Category, SavedKnowledgeView

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



class SavedKnowledgeViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.category = create_category_path("학습/언어/영어")

    def post(self, data):
        return self.client.post(
            "/api/saved-knowledge-views/",
            data=json.dumps(data),
            content_type="application/json",
        )

    def patch(self, view_id: int, data):
        return self.client.patch(
            f"/api/saved-knowledge-views/{view_id}/",
            data=json.dumps(data),
            content_type="application/json",
        )

    def test_crud_canonicalizes_filters_and_switches_single_default(self):
        first = self.post(
            {
                "name": " 읽을 보기 ",
                "filters": {
                    "category": self.category.pk,
                    "read": "unread",
                    "sort": "oldest",
                },
                "is_default": True,
            }
        )
        second = self.post(
            {
                "name": "두 번째",
                "filters": {"completed": "1"},
                "is_default": True,
            }
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["name"], "읽을 보기")
        self.assertEqual(first.json()["filters"], {"category": self.category.pk, "read": "unread"})
        self.assertEqual(first.json()["sort"], "oldest")
        self.assertEqual(second.status_code, 201)
        self.assertEqual(SavedKnowledgeView.objects.filter(default_slot=1).count(), 1)
        self.assertEqual(
            SavedKnowledgeView.objects.get(pk=second.json()["id"]).default_slot,
            1,
        )
        self.assertIsNone(
            SavedKnowledgeView.objects.get(pk=first.json()["id"]).default_slot
        )

        renamed = self.patch(first.json()["id"], {"name": "수정된 보기"})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["name"], "수정된 보기")
        listed = self.client.get("/api/saved-knowledge-views/").json()["results"]
        self.assertEqual(len(listed), 2)
        deleted = self.client.delete(
            f"/api/saved-knowledge-views/{first.json()['id']}/"
        )
        self.assertEqual(deleted.status_code, 204)

    def test_identity_is_unicode_case_and_space_independent_but_accent_distinct(self):
        first = self.post({"name": "Café", "filters": {}})
        duplicate = self.post({"name": "  CAFE\u0301  ", "filters": {}})
        accent_distinct = self.post({"name": "Cafe", "filters": {}})

        self.assertEqual(first.status_code, 201)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["code"], "duplicate_name")
        self.assertEqual(accent_distinct.status_code, 201)
        self.assertEqual(SavedKnowledgeView.objects.count(), 2)
        self.assertNotEqual(
            SavedKnowledgeView.objects.get(pk=first.json()["id"]).identity_hash,
            SavedKnowledgeView.objects.get(pk=accent_distinct.json()["id"]).identity_hash,
        )

    def test_apply_reports_stale_category_but_row_remains_editable_and_deletable(self):
        created = self.post(
            {
                "name": "카테고리 보기",
                "filters": {"category": self.category.pk},
            }
        )
        self.category.is_active = False
        self.category.save(update_fields=["is_active", "updated_at"])

        applied = self.client.get(
            f"/api/saved-knowledge-views/{created.json()['id']}/apply/"
        )
        renamed = self.patch(created.json()["id"], {"name": "복구할 보기"})
        deleted = self.client.delete(
            f"/api/saved-knowledge-views/{created.json()['id']}/"
        )

        self.assertEqual(applied.status_code, 400)
        self.assertEqual(applied.json()["code"], "stale_category")
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(deleted.status_code, 204)

    def test_database_constraints_reject_invalid_or_duplicate_default_slots(self):
        SavedKnowledgeView.objects.create(
            name="기본",
            normalized_name="기본",
            canonical_filters={},
            default_slot=1,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SavedKnowledgeView.objects.create(
                name="중복 기본",
                normalized_name="중복 기본",
                canonical_filters={},
                default_slot=1,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SavedKnowledgeView.objects.create(
                name="잘못된 슬롯",
                normalized_name="잘못된 슬롯",
                canonical_filters={},
                default_slot=2,
            )

    def test_database_identity_constraint_rejects_canonical_duplicate(self):
        SavedKnowledgeView.objects.create(name="Café", canonical_filters={})

        with self.assertRaises(IntegrityError), transaction.atomic():
            SavedKnowledgeView.objects.create(name="CAFE\u0301", canonical_filters={})
