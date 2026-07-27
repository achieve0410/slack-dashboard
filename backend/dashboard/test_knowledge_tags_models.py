from datetime import UTC, datetime

from django.db import IntegrityError, transaction
from django.test import TestCase

from .models import (
    Category,
    ContentRun,
    CronJob,
    KnowledgeItem,
    KnowledgeTag,
    KnowledgeTagActiveSnapshot,
    KnowledgeTagAssignment,
    KnowledgeTagMutationLock,
    KnowledgeTagSnapshot,
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


def create_knowledge_item(*, category: Category) -> KnowledgeItem:
    job = CronJob.objects.create(external_id="tag-source", name="tag-source")
    run = ContentRun.objects.create(
        job=job,
        status=ContentRun.Status.SUCCESS,
        title="Tag source",
        body="source body",
        generated_at=NOW,
    )
    return KnowledgeItem.objects.create(
        source_type=KnowledgeItem.SourceType.CRON,
        source_key=f"cron:{run.pk}",
        content_run=run,
        category=category,
        status=KnowledgeItem.Status.CLASSIFIED,
        title="Taggable item",
        summary="summary",
        source_hash="a" * 64,
        generated_at=NOW,
        classified_at=NOW,
    )


class KnowledgeTagModelTests(TestCase):
    def setUp(self):
        self.category = create_category_path("업무/운영/관측")
        self.item = create_knowledge_item(category=self.category)

    def test_exact_normalized_duplicate_tags_collapse_without_semantic_merge(self):
        tag = KnowledgeTag.for_label("  Security  ")

        self.assertEqual(tag.label, "Security")
        self.assertEqual(tag.normalized_label, "security")
        self.assertEqual(len(tag.identity_hash), 64)
        self.assertEqual(KnowledgeTag.for_label("Security"), tag)
        self.assertEqual(KnowledgeTag.for_label("security"), tag)

        with self.assertRaises(IntegrityError), transaction.atomic():
            KnowledgeTag.objects.create(label="security")

        semantic_overlap = KnowledgeTag.for_label("AWS 보안")

        self.assertNotEqual(tag.identity_hash, semantic_overlap.identity_hash)

    def test_snapshot_assignments_have_no_three_tag_upper_bound(self):
        snapshot = KnowledgeTagSnapshot.objects.create(
            status=KnowledgeTagSnapshot.Status.ACTIVE,
            inventory_digest="b" * 64,
            item_count=1,
            tag_count=4,
            assignment_count=4,
            published_at=NOW,
        )
        tags = [
            KnowledgeTag.for_label(label)
            for label in ("보안", "Security", "AWS 보안", "운영")
        ]

        for position, tag in enumerate(tags, start=1):
            KnowledgeTagAssignment.objects.create(
                snapshot=snapshot,
                knowledge_item=self.item,
                tag=tag,
                position=position,
            )

        self.assertEqual(
            KnowledgeTagAssignment.objects.filter(
                snapshot=snapshot,
                knowledge_item=self.item,
            ).count(),
            4,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            KnowledgeTagAssignment.objects.create(
                snapshot=snapshot,
                knowledge_item=self.item,
                tag=tags[0],
                position=5,
            )

    def test_active_pointer_and_mutation_lock_are_singletons(self):
        first = KnowledgeTagActiveSnapshot.objects.get(singleton_key=1)
        replacement = KnowledgeTagSnapshot.objects.create(
            status=KnowledgeTagSnapshot.Status.ACTIVE,
            inventory_digest="c" * 64,
            published_at=NOW,
        )
        first.snapshot = replacement
        first.save()

        with self.assertRaises(IntegrityError), transaction.atomic():
            KnowledgeTagActiveSnapshot.objects.create(
                singleton_key=1,
                snapshot=KnowledgeTagSnapshot.objects.create(),
            )

        with transaction.atomic():
            lock = KnowledgeTagMutationLock.lock()

        self.assertEqual(lock.singleton_key, 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            KnowledgeTagMutationLock.objects.create(singleton_key=1)

    def test_bootstrap_migration_does_not_modify_existing_knowledge_rows(self):
        self.assertEqual(KnowledgeItem.objects.count(), 1)
        self.assertEqual(KnowledgeTagSnapshot.objects.count(), 1)
        self.assertEqual(KnowledgeTagActiveSnapshot.objects.count(), 1)
        self.assertEqual(KnowledgeTagMutationLock.objects.count(), 1)
        self.assertEqual(KnowledgeTagAssignment.objects.count(), 0)
