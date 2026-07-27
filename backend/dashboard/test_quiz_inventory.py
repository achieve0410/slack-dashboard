from datetime import UTC, datetime

from django.test import TestCase
from django.utils import timezone

from .models import (
    Category,
    ContentRun,
    CronJob,
    KnowledgeConsumptionState,
    KnowledgeItem,
)
from .quiz_inventory import collect_quiz_inventory


NOW = datetime(2026, 7, 21, tzinfo=UTC)


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


class QuizInventoryTests(TestCase):
    def setUp(self):
        self.english = create_category_path("학습/언어/영어")
        self.japanese = create_category_path("학습/언어/일본어")
        self.aws = create_category_path("학습/자격증/AWS")
        self.other = create_category_path("학습/자격증/Azure")

    def create_cron_item(
        self,
        *,
        external_id: str,
        category: Category,
        source_hash: str,
        body: str = "source body",
        status: str = ContentRun.Status.SUCCESS,
        hidden_item=False,
        hidden_run=False,
        archived=False,
    ) -> KnowledgeItem:
        job = CronJob.objects.create(
            external_id=external_id,
            name=external_id,
            category=CronJob.Category.OTHER,
        )
        run = ContentRun.objects.create(
            job=job,
            status=status,
            title=f"{external_id} title",
            body=body,
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

    def create_slack_item(self, *, category: Category, suffix: str) -> KnowledgeItem:
        return KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key=f"slack:quiz:{suffix}",
            category=category,
            status=KnowledgeItem.Status.CLASSIFIED,
            title=f"Slack {suffix}",
            summary="summary",
            question="question",
            answer="answer",
            source_hash=suffix[0] * 64,
            generated_at=NOW,
            classified_at=NOW,
        )

    def test_exact_supported_paths_visible_sources_and_aws_allowlist(self):
        english = self.create_cron_item(
            external_id="english",
            category=self.english,
            source_hash="a" * 64,
        )
        japanese = self.create_slack_item(category=self.japanese, suffix="japanese")
        aws_allowed = self.create_cron_item(
            external_id="aws-saa-allowed",
            category=self.aws,
            source_hash="b" * 64,
        )
        aws_blocked = self.create_cron_item(
            external_id="aws-mixed",
            category=self.aws,
            source_hash="c" * 64,
        )
        aws_slack = self.create_slack_item(category=self.aws, suffix="aws")
        self.create_cron_item(
            external_id="hidden-item",
            category=self.english,
            source_hash="d" * 64,
            hidden_item=True,
        )
        self.create_cron_item(
            external_id="hidden-run",
            category=self.english,
            source_hash="e" * 64,
            hidden_run=True,
        )
        self.create_cron_item(
            external_id="archived",
            category=self.english,
            source_hash="f" * 64,
            archived=True,
        )
        self.create_cron_item(
            external_id="empty",
            category=self.english,
            source_hash="0" * 64,
            body="",
        )
        self.create_cron_item(
            external_id="azure",
            category=self.other,
            source_hash="1" * 64,
        )

        inventory = collect_quiz_inventory(
            aws_allowlisted_external_ids=["aws-saa-allowed"],
        )

        self.assertEqual(
            [(candidate.domain, candidate.source_key) for candidate in inventory.eligible],
            [
                ("english", english.source_key),
                ("japanese", japanese.source_key),
                ("aws_saa", aws_allowed.source_key),
            ],
        )
        self.assertEqual(
            [(candidate.source_key, candidate.reason) for candidate in inventory.quarantined],
            [
                (aws_blocked.source_key, "aws_cron_not_allowlisted"),
                (aws_slack.source_key, "aws_slack_qa_requires_explicit_review"),
            ],
        )
        self.assertEqual(len(inventory.inventory_version), 64)

    def test_aws_source_key_allowlist_is_explicit_and_deterministic(self):
        aws = self.create_cron_item(
            external_id="aws-by-source-key",
            category=self.aws,
            source_hash="b" * 64,
        )

        blocked = collect_quiz_inventory()
        allowed = collect_quiz_inventory(aws_allowlisted_source_keys=[aws.source_key])
        allowed_again = collect_quiz_inventory(aws_allowlisted_source_keys=[aws.source_key])

        self.assertEqual(len(blocked.eligible), 0)
        self.assertEqual(blocked.quarantined[0].source_key, aws.source_key)
        self.assertEqual(allowed.eligible[0].source_key, aws.source_key)
        self.assertEqual(allowed, allowed_again)
