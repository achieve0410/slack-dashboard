import hashlib
import json
import threading
import time
from datetime import UTC, datetime
from unittest.mock import patch

from django.db import close_old_connections, connection, connections
from django.test import Client, TransactionTestCase

from . import quiz_sessions
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
            defaults={"name": name, "parent": parent, "depth": depth},
        )
        parent = category
    return category


class QuizAnswerConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        category = create_category_path("학습/언어/영어")
        batch = QuizGenerationBatch.objects.create(
            inventory_version="v1",
            dry_run=False,
            status=QuizGenerationBatch.Status.SUCCESS,
            generator_version="quizgen-v1",
        )
        questions = []
        for index in range(10):
            suffix = f"race-{index}"
            job = CronJob.objects.create(
                external_id=suffix,
                name=suffix,
                category=CronJob.Category.OTHER,
            )
            run = ContentRun.objects.create(
                job=job,
                status=ContentRun.Status.SUCCESS,
                title=suffix,
                body=suffix,
                generated_at=NOW,
            )
            item = KnowledgeItem.objects.create(
                source_type=KnowledgeItem.SourceType.CRON,
                source_key=f"cron:{run.pk}",
                content_run=run,
                category=category,
                status=KnowledgeItem.Status.CLASSIFIED,
                title=suffix,
                summary=suffix,
                source_hash=hashlib.sha256(suffix.encode()).hexdigest(),
                generated_at=NOW,
                classified_at=NOW,
            )
            questions.append(
                QuizQuestion.objects.create(
                    batch=batch,
                    knowledge_item=item,
                    domain="english",
                    difficulty="beginner",
                    question_type="single_choice",
                    prompt=f"Prompt {index}",
                    choices=[{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
                    correct_choice_ids=["a"],
                    explanation="Explanation",
                    evidence_excerpt=suffix,
                    evidence_digest="d" * 64,
                    source_hash=item.source_hash,
                    generator_version="quizgen-v1",
                    prompt_version="prompt-v1",
                    prompt_digest="e" * 64,
                    publish_state=QuizQuestion.PublishState.PUBLISHED,
                    is_active=True,
                    published_at=NOW,
                )
            )
        self.session = QuizSession.objects.create(
            domain="english",
            difficulty="beginner",
            mode="new",
            required_count=10,
        )
        QuizSessionItem.objects.bulk_create(
            [
                QuizSessionItem(session=self.session, question=question, position=index)
                for index, question in enumerate(questions, start=1)
            ]
        )
        self.item = QuizSessionItem.objects.get(session=self.session, position=1)

    def test_conflicting_first_submit_is_single_winner_mysql(self):
        if connection.vendor != "mysql":
            self.skipTest("MySQL row-lock quiz answer regression")

        first_inside = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        results = {}
        errors = []
        original_update_progress = quiz_sessions.update_progress

        def blocking_update_progress(question, correct, answered_at, *, mode="new"):
            result = original_update_progress(
                question,
                correct,
                answered_at,
                mode=mode,
            )
            if threading.current_thread().name == "quiz-first-answer":
                first_inside.set()
                if not release_first.wait(10):
                    raise TimeoutError("first answer release timed out")
            return result

        def answer(name, choice_ids):
            close_old_connections()
            try:
                if name == "second":
                    second_started.set()
                response = Client().post(
                    f"/api/quiz/sessions/{self.session.session_id}/items/{self.item.pk}/answer/",
                    data=json.dumps({"choice_ids": choice_ids}),
                    content_type="application/json",
                )
                results[name] = (response.status_code, response.json())
            except Exception as error:
                errors.append(error)
            finally:
                connections["default"].close()

        with patch.object(
            quiz_sessions,
            "update_progress",
            side_effect=blocking_update_progress,
        ):
            first = threading.Thread(
                target=answer,
                args=("first", ["a"]),
                name="quiz-first-answer",
            )
            second = threading.Thread(
                target=answer,
                args=("second", ["b"]),
                name="quiz-second-answer",
            )
            first.start()
            self.assertTrue(first_inside.wait(10))
            second.start()
            self.assertTrue(second_started.wait(10))
            time.sleep(0.2)
            self.assertTrue(second.is_alive())
            release_first.set()
            first.join(10)
            second.join(10)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        statuses = sorted([results["first"][0], results["second"][0]])
        self.assertEqual(statuses, [200, 409])
        self.item.refresh_from_db()
        self.assertEqual(self.item.accepted_choice_ids, ["a"])
        self.assertEqual(QuizProgress.objects.count(), 1)
        self.assertEqual(QuizProgress.objects.get().correct_streak, 1)
