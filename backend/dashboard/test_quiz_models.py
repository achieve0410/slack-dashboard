from datetime import UTC, datetime

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from .models import (
    Category,
    ContentRun,
    CronJob,
    KnowledgeItem,
    QuizGenerationBatch,
    QuizProgress,
    QuizQuestion,
    QuizSession,
    QuizSessionItem,
)


NOW = datetime(2026, 7, 21, tzinfo=UTC)


def create_category_path(path: str) -> Category:
    parent = None
    pieces = []
    category = None
    for depth, name in enumerate(path.split("/"), start=1):
        pieces.append(name)
        category, _ = Category.objects.get_or_create(
            path="/".join(pieces),
            defaults={
                "name": name,
                "parent": parent,
                "depth": depth,
            },
        )
        parent = category
    return category


class QuizModelTests(TestCase):
    def setUp(self):
        self.category = create_category_path("학습/언어/영어")
        self.job = CronJob.objects.create(
            external_id="english-quiz",
            name="English Quiz",
            category=CronJob.Category.OTHER,
        )
        self.run = ContentRun.objects.create(
            job=self.job,
            status=ContentRun.Status.SUCCESS,
            title="English source",
            body="English body",
            generated_at=NOW,
        )
        self.item = KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.CRON,
            source_key=f"cron:{self.run.pk}",
            content_run=self.run,
            category=self.category,
            status=KnowledgeItem.Status.CLASSIFIED,
            title="English source",
            summary="English summary",
            source_hash="a" * 64,
            generated_at=NOW,
            classified_at=NOW,
        )
        self.batch = QuizGenerationBatch.objects.create(
            inventory_version="inventory-v1",
            allowlist_snapshot={"aws_external_ids": []},
            dry_run=False,
            status=QuizGenerationBatch.Status.SUCCESS,
            candidate_count=1,
            published_count=1,
            candidate_outcomes=[{"source_key": self.item.source_key, "status": "published"}],
            generator_version="quizgen-v1",
            model_name="test-model",
            prompt_version="prompt-v1",
            prompt_digest="b" * 64,
            started_at=NOW,
            finished_at=NOW,
        )

    def build_question(self, **overrides) -> QuizQuestion:
        values = {
            "batch": self.batch,
            "knowledge_item": self.item,
            "domain": QuizQuestion.Domain.ENGLISH,
            "difficulty": QuizQuestion.Difficulty.BEGINNER,
            "question_type": QuizQuestion.QuestionType.SINGLE_CHOICE,
            "prompt": "Choose the correct meaning.",
            "choices": [
                {"id": "a", "text": "Correct"},
                {"id": "b", "text": "Wrong"},
            ],
            "correct_choice_ids": ["a"],
            "explanation": "Because the source says so.",
            "evidence_excerpt": "source excerpt",
            "evidence_digest": "c" * 64,
            "source_hash": self.item.source_hash,
            "generator_version": "quizgen-v1",
            "model_name": "test-model",
            "prompt_version": "prompt-v1",
            "prompt_digest": "d" * 64,
        }
        values.update(overrides)
        return QuizQuestion(**values)

    def test_question_validates_choice_shape_and_correct_ids(self):
        invalid_cases = (
            {"choices": [{"id": "a", "text": "A"}, {"id": "a", "text": "B"}]},
            {"correct_choice_ids": ["a", "a"]},
            {"correct_choice_ids": ["missing"]},
            {
                "question_type": QuizQuestion.QuestionType.MULTIPLE_SELECT,
                "correct_choice_ids": ["a"],
            },
        )
        for overrides in invalid_cases:
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                self.build_question(**overrides).full_clean()

        question = self.build_question(
            question_type=QuizQuestion.QuestionType.MULTIPLE_SELECT,
            choices=[
                {"id": "a", "text": "Correct"},
                {"id": "b", "text": "Also correct"},
                {"id": "c", "text": "Wrong"},
            ],
            correct_choice_ids=["a", "b"],
        )
        question.full_clean()

    def test_published_question_content_is_immutable_and_versions_are_retained(self):
        published = self.build_question(
            publish_state=QuizQuestion.PublishState.PUBLISHED,
            is_active=True,
            published_at=timezone.now(),
        )
        published.save()

        published.prompt = "Changed after publish"
        with self.assertRaises(ValidationError):
            published.save()

        replacement = self.build_question(
            source_hash="e" * 64,
            prompt="Replacement question",
            publish_state=QuizQuestion.PublishState.PUBLISHED,
            is_active=True,
            published_at=timezone.now(),
        )
        replacement.save()
        published.refresh_from_db()
        published.publish_state = QuizQuestion.PublishState.SUPERSEDED
        published.is_active = False
        published.superseded_by = replacement
        published.save(
            update_fields=[
                "publish_state",
                "is_active",
                "superseded_by",
                "active_identity_hash",
                "updated_at",
            ]
        )

        self.assertEqual(QuizQuestion.objects.count(), 2)
        self.assertEqual(
            replacement.superseded_versions.get(),
            published,
        )

    def test_active_identity_allows_distinct_questions_and_blocks_exact_duplicates(self):
        first = self.build_question(
            publish_state=QuizQuestion.PublishState.PUBLISHED,
            is_active=True,
            published_at=timezone.now(),
        )
        first.save()
        second = self.build_question(
            prompt="Choose the incorrect meaning.",
            publish_state=QuizQuestion.PublishState.PUBLISHED,
            is_active=True,
            published_at=timezone.now(),
        )
        second.save()
        duplicate = self.build_question(
            choices=[
                {"id": "b", "text": "Wrong"},
                {"id": "a", "text": "Correct"},
            ],
            publish_state=QuizQuestion.PublishState.PUBLISHED,
            is_active=True,
            published_at=timezone.now(),
        )

        self.assertNotEqual(first.question_fingerprint, second.question_fingerprint)
        self.assertEqual(QuizQuestion.objects.filter(is_active=True).count(), 2)
        with self.assertRaises(ValidationError):
            duplicate.save()

    def test_exact_duplicate_can_replace_active_question_after_superseding_old_row(self):
        old = self.build_question(
            publish_state=QuizQuestion.PublishState.PUBLISHED,
            is_active=True,
            published_at=timezone.now(),
        )
        old.save()
        replacement = self.build_question(
            publish_state=QuizQuestion.PublishState.PUBLISHED,
            is_active=False,
            published_at=timezone.now(),
        )
        replacement.save()

        self.assertEqual(old.question_fingerprint, replacement.question_fingerprint)
        with transaction.atomic():
            old.publish_state = QuizQuestion.PublishState.SUPERSEDED
            old.is_active = False
            old.superseded_by = replacement
            old.save(
                update_fields=[
                    "publish_state",
                    "is_active",
                    "superseded_by",
                    "active_identity_hash",
                    "updated_at",
                ]
            )
            replacement.is_active = True
            replacement.save(
                update_fields=["is_active", "active_identity_hash", "updated_at"]
            )

        old.refresh_from_db()
        replacement.refresh_from_db()
        self.assertEqual(old.publish_state, QuizQuestion.PublishState.SUPERSEDED)
        self.assertIsNone(old.active_identity_hash)
        self.assertTrue(replacement.is_active)
        self.assertEqual(QuizQuestion.objects.filter(is_active=True).get(), replacement)

    def test_owner_only_schema_has_no_user_foreign_keys(self):
        user_targets = {"auth.User", "settings.AUTH_USER_MODEL"}
        for model in (
            QuizGenerationBatch,
            QuizQuestion,
            QuizSession,
            QuizSessionItem,
            QuizProgress,
        ):
            for field in model._meta.fields:
                remote = getattr(field.remote_field, "model", None)
                self.assertNotIn(str(remote), user_targets)

    def test_session_item_and_progress_uniqueness(self):
        question = self.build_question(
            publish_state=QuizQuestion.PublishState.PUBLISHED,
            is_active=True,
            published_at=timezone.now(),
        )
        question.save()
        other_question = self.build_question(
            source_hash="f" * 64,
            prompt="Second question",
            publish_state=QuizQuestion.PublishState.PUBLISHED,
            is_active=True,
            published_at=timezone.now(),
        )
        other_question.save()
        session = QuizSession.objects.create(
            domain=QuizSession.Domain.ENGLISH,
            difficulty=QuizSession.Difficulty.BEGINNER,
        )
        QuizSessionItem.objects.create(session=session, question=question, position=1)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            QuizSessionItem.objects.create(session=session, question=question, position=2)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            QuizSessionItem.objects.create(session=session, question=other_question, position=1)

        QuizProgress.objects.create(question=question)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            QuizProgress.objects.create(question=question)
