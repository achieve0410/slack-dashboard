from datetime import UTC, datetime

from django.test import TestCase
from django.utils import timezone

from .knowledge_tag_inventory import collect_knowledge_tag_inventory
from .models import (
    Category,
    ContentRun,
    CronJob,
    KnowledgeConsumptionState,
    KnowledgeItem,
    KnowledgeTagCorpusRevision,
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


class KnowledgeTagInventoryTests(TestCase):
    def setUp(self):
        self.english = create_category_path("학습/언어/영어")
        self.japanese = create_category_path("학습/언어/일본어")
        self.aws = create_category_path("학습/자격증/AWS")
        self.ops = create_category_path("업무/운영/장애")

    def create_cron_item(
        self,
        *,
        external_id: str,
        category: Category,
        source_hash: str,
        hidden_item=False,
        hidden_run=False,
        archived=False,
    ) -> KnowledgeItem:
        job = CronJob.objects.create(external_id=external_id, name=external_id)
        run = ContentRun.objects.create(
            job=job,
            status=ContentRun.Status.SUCCESS,
            title=f"{external_id} title",
            body="source body",
            generated_at=NOW,
            hidden_at=timezone.now() if hidden_run else None,
        )
        item = KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.CRON,
            source_key=f"cron:{run.pk}",
            content_run=run,
            category=category,
            status=KnowledgeItem.Status.CLASSIFIED,
            title=f"{external_id} title",
            summary="summary",
            source_hash=source_hash,
            generated_at=NOW,
            classified_at=NOW,
            hidden_at=timezone.now() if hidden_item else None,
        )
        if archived:
            KnowledgeConsumptionState.objects.create(
                knowledge_item=item,
                archived_at=timezone.now(),
            )
        return item

    def create_slack_item(
        self,
        *,
        suffix: str,
        status: str,
        category: Category | None = None,
        answer: str = "answer",
    ) -> KnowledgeItem:
        return KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key=f"slack:tag:{suffix}",
            category=category,
            status=status,
            title=f"Slack {suffix}",
            summary="summary",
            question="question",
            answer=answer,
            source_hash=suffix[0] * 64,
            generated_at=NOW,
            classified_at=NOW if status == KnowledgeItem.Status.CLASSIFIED else None,
        )

    def test_inventory_includes_all_visible_non_learning_knowledge(self):
        classified = self.create_cron_item(
            external_id="classified",
            category=self.ops,
            source_hash="a" * 64,
        )
        archived = self.create_cron_item(
            external_id="archived",
            category=self.ops,
            source_hash="b" * 64,
            archived=True,
        )
        awaiting = self.create_slack_item(
            suffix="awaiting",
            status=KnowledgeItem.Status.AWAITING_ANSWER,
            answer="",
        )
        pending = self.create_slack_item(
            suffix="pending",
            status=KnowledgeItem.Status.PENDING,
        )
        needs_review = self.create_slack_item(
            suffix="review",
            status=KnowledgeItem.Status.NEEDS_REVIEW,
        )

        self.create_cron_item(
            external_id="english",
            category=self.english,
            source_hash="c" * 64,
        )
        self.create_cron_item(
            external_id="japanese",
            category=self.japanese,
            source_hash="d" * 64,
        )
        self.create_cron_item(
            external_id="aws",
            category=self.aws,
            source_hash="e" * 64,
        )
        self.create_cron_item(
            external_id="hidden-item",
            category=self.ops,
            source_hash="f" * 64,
            hidden_item=True,
        )
        self.create_cron_item(
            external_id="hidden-run",
            category=self.ops,
            source_hash="1" * 64,
            hidden_run=True,
        )

        inventory = collect_knowledge_tag_inventory()

        self.assertEqual(
            [item.source_key for item in inventory.eligible],
            [
                classified.source_key,
                archived.source_key,
                awaiting.source_key,
                pending.source_key,
                needs_review.source_key,
            ],
        )
        self.assertEqual(len(inventory.inventory_digest), 64)

    def test_inventory_digest_is_deterministic_and_changes_with_membership(self):
        self.create_cron_item(
            external_id="first",
            category=self.ops,
            source_hash="a" * 64,
        )

        first = collect_knowledge_tag_inventory()
        second = collect_knowledge_tag_inventory()
        self.assertEqual(first, second)

        self.create_cron_item(
            external_id="second",
            category=self.ops,
            source_hash="b" * 64,
        )
        changed = collect_knowledge_tag_inventory()

        self.assertNotEqual(first.inventory_digest, changed.inventory_digest)

    def test_inventory_digest_changes_when_only_title_changes(self):
        item = self.create_cron_item(
            external_id="title-drift",
            category=self.ops,
            source_hash="a" * 64,
        )
        before = collect_knowledge_tag_inventory()

        item.title = "Retitled without source hash change"
        item.save(update_fields=["title"])
        after = collect_knowledge_tag_inventory()

        self.assertNotEqual(before.inventory_digest, after.inventory_digest)

    def test_corpus_revision_fence_tracks_insert_and_linked_drift(self):
        before_revision = KnowledgeTagCorpusRevision.get_current().revision
        item = self.create_cron_item(
            external_id="revision-first",
            category=self.ops,
            source_hash="a" * 64,
        )
        after_insert = KnowledgeTagCorpusRevision.get_current().revision

        item.content_run.body = "changed source body"
        item.content_run.save(update_fields=["body", "updated_at"])
        after_content = KnowledgeTagCorpusRevision.get_current().revision

        self.ops.name = "장애 대응"
        self.ops.path = "업무/운영/장애 대응"
        self.ops.path_key = Category.canonical_path_key(self.ops.path)
        self.ops.save(update_fields=["name", "path", "path_key"])
        after_category = KnowledgeTagCorpusRevision.get_current().revision

        self.assertGreater(after_insert, before_revision)
        self.assertGreater(after_content, after_insert)
        self.assertGreater(after_category, after_content)

    def test_corpus_revision_fence_tracks_linked_content_delete(self):
        item = self.create_cron_item(
            external_id="revision-delete",
            category=self.ops,
            source_hash="a" * 64,
        )
        before_delete = KnowledgeTagCorpusRevision.get_current().revision

        item.content_run.delete()

        after_delete = KnowledgeTagCorpusRevision.get_current().revision
        self.assertGreater(after_delete, before_delete)
