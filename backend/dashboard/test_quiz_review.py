import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.test import Client, TestCase
from django.utils import timezone

from .models import (
    Category,
    ContentRun,
    CronJob,
    KnowledgeConsumptionState,
    KnowledgeItem,
    QuizGenerationBatch,
    QuizProgress,
    QuizQuestion,
    QuizSession,
    QuizSessionItem,
)
from .quiz_review import apply_answer_progress


NOW = datetime(2026, 7, 21, tzinfo=UTC)
FORBIDDEN_REVIEW = {
    "accepted_choice_ids",
    "correct_choice_ids",
    "explanation",
    "evidence_excerpt",
    "evidence_digest",
    "source_key",
    "source_hash",
    "body",
}
FORBIDDEN_RESULT = {"evidence_excerpt", "evidence_digest", "source_hash"}


def assert_forbidden_absent(testcase, payload, forbidden=FORBIDDEN_REVIEW):
    if isinstance(payload, dict):
        testcase.assertFalse(forbidden & set(payload), forbidden & set(payload))
        for value in payload.values():
            assert_forbidden_absent(testcase, value, forbidden)
    elif isinstance(payload, list):
        for value in payload:
            assert_forbidden_absent(testcase, value, forbidden)


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


class QuizReviewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.english = create_category_path("학습/언어/영어")
        self.aws = create_category_path("학습/자격증/AWS")
        self.batch = QuizGenerationBatch.objects.create(
            inventory_version="v1",
            dry_run=False,
            status=QuizGenerationBatch.Status.SUCCESS,
            generator_version="quizgen-v1",
        )

    def create_item(self, suffix: str, *, category=None, hidden=False, archived=False):
        category = category or self.english
        job = CronJob.objects.create(
            external_id=f"review-{suffix}",
            name=f"Review {suffix}",
            category=CronJob.Category.OTHER,
        )
        run = ContentRun.objects.create(
            job=job,
            status=ContentRun.Status.SUCCESS,
            title=f"Source {suffix}",
            body=f"Body {suffix}",
            generated_at=NOW,
        )
        item = KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.CRON,
            source_key=f"cron:{run.pk}",
            content_run=run,
            category=category,
            status=KnowledgeItem.Status.CLASSIFIED,
            title=f"Source {suffix}",
            summary=f"Summary {suffix}",
            source_hash=hashlib.sha256(suffix.encode()).hexdigest(),
            generated_at=NOW,
            classified_at=NOW,
            hidden_at=timezone.now() if hidden else None,
        )
        if archived:
            KnowledgeConsumptionState.objects.create(
                knowledge_item=item,
                archived_at=timezone.now(),
            )
        return item

    def create_question(
        self,
        suffix: str,
        *,
        domain="english",
        difficulty="beginner",
        item=None,
        active=True,
        source_hash=None,
    ):
        item = item or self.create_item(
            suffix,
            category=self.aws if domain == "aws_saa" else self.english,
        )
        return QuizQuestion.objects.create(
            batch=self.batch,
            knowledge_item=item,
            domain=domain,
            difficulty=difficulty,
            question_type=QuizQuestion.QuestionType.SINGLE_CHOICE,
            prompt=f"Prompt {suffix}",
            choices=[{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
            correct_choice_ids=["a"],
            explanation=f"Explanation {suffix}",
            evidence_excerpt="Body",
            evidence_digest="d" * 64,
            source_hash=source_hash or item.source_hash,
            generator_version="quizgen-v1",
            prompt_version="prompt-v1",
            prompt_digest="e" * 64,
            publish_state=QuizQuestion.PublishState.PUBLISHED,
            is_active=active,
            published_at=timezone.now(),
        )

    def seed_bank(self, count=10):
        return [self.create_question(f"bank-{index}") for index in range(count)]

    def post_json(self, url, payload):
        return self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def patch_json(self, url, payload):
        return self.client.patch(
            url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_review_stage_transitions_are_fixed_and_due_only(self):
        question = self.create_question("stage")
        answered_at = datetime(2026, 7, 21, 12, tzinfo=UTC)

        for current_stage, next_stage in (
            (QuizProgress.Stage.RESET, QuizProgress.Stage.THREE_DAYS),
            (QuizProgress.Stage.ONE_DAY, QuizProgress.Stage.THREE_DAYS),
            (QuizProgress.Stage.THREE_DAYS, QuizProgress.Stage.SEVEN_DAYS),
            (QuizProgress.Stage.SEVEN_DAYS, QuizProgress.Stage.FOURTEEN_DAYS),
            (QuizProgress.Stage.FOURTEEN_DAYS, QuizProgress.Stage.THIRTY_DAYS),
        ):
            with self.subTest(stage=current_stage):
                QuizProgress.objects.update_or_create(
                    question=question,
                    defaults={
                        "stage": current_stage,
                        "next_review_at": answered_at - timedelta(minutes=1),
                        "correct_streak": 0,
                    },
                )
                progress = apply_answer_progress(
                    question,
                    correct=True,
                    answered_at=answered_at,
                    mode=QuizSession.Mode.REVIEW,
                )
                saved = QuizProgress.objects.get(question=question)
                self.assertEqual(saved.stage, next_stage)
                self.assertEqual(progress["correct_streak"], 1)
                self.assertEqual(
                    saved.next_review_at,
                    answered_at + timedelta(days=int(next_stage.rstrip("d"))),
                )

        QuizProgress.objects.update_or_create(
            question=question,
            defaults={
                "stage": QuizProgress.Stage.THIRTY_DAYS,
                "next_review_at": answered_at - timedelta(minutes=1),
                "mastered_at": None,
            },
        )
        apply_answer_progress(
            question,
            correct=True,
            answered_at=answered_at,
            mode=QuizSession.Mode.REVIEW,
        )
        saved = QuizProgress.objects.get(question=question)
        self.assertEqual(saved.stage, QuizProgress.Stage.THIRTY_DAYS)
        self.assertEqual(saved.mastered_at, answered_at)
        self.assertIsNone(saved.next_review_at)

        QuizProgress.objects.update_or_create(
            question=question,
            defaults={
                "stage": QuizProgress.Stage.THREE_DAYS,
                "next_review_at": answered_at + timedelta(minutes=1),
                "mastered_at": None,
            },
        )
        apply_answer_progress(
            question,
            correct=True,
            answered_at=answered_at,
            mode=QuizSession.Mode.REVIEW,
        )
        saved = QuizProgress.objects.get(question=question)
        self.assertEqual(saved.stage, QuizProgress.Stage.THREE_DAYS)

        QuizProgress.objects.update_or_create(
            question=question,
            defaults={
                "stage": QuizProgress.Stage.THREE_DAYS,
                "next_review_at": answered_at - timedelta(minutes=1),
            },
        )
        apply_answer_progress(
            question,
            correct=True,
            answered_at=answered_at,
            mode=QuizSession.Mode.NEW,
        )
        saved = QuizProgress.objects.get(question=question)
        self.assertEqual(saved.stage, QuizProgress.Stage.THREE_DAYS)

        apply_answer_progress(
            question,
            correct=False,
            answered_at=answered_at,
            mode=QuizSession.Mode.REVIEW,
        )
        saved = QuizProgress.objects.get(question=question)
        self.assertEqual(saved.stage, QuizProgress.Stage.RESET)
        self.assertEqual(saved.wrong_count, 1)
        self.assertEqual(saved.correct_streak, 0)
        self.assertEqual(saved.next_review_at, answered_at + timedelta(days=1))
        self.assertIsNone(saved.mastered_at)

    def test_review_endpoint_filters_orders_and_hides_answers(self):
        q1 = self.create_question("due-late")
        q2 = self.create_question("due-early")
        q3 = self.create_question("future", difficulty="intermediate")
        blank = self.create_question("blank")
        hidden = self.create_question("hidden", item=self.create_item("hidden", hidden=True))
        archived = self.create_question(
            "archived",
            item=self.create_item("archived", archived=True),
        )
        stale_item = self.create_item("stale")
        stale = self.create_question("stale", item=stale_item, source_hash="f" * 64)
        now = datetime(2026, 7, 21, 12, tzinfo=UTC)
        QuizProgress.objects.create(
            question=q1,
            stage=QuizProgress.Stage.THREE_DAYS,
            next_review_at=now - timedelta(hours=1),
        )
        QuizProgress.objects.create(
            question=q2,
            stage=QuizProgress.Stage.ONE_DAY,
            next_review_at=now - timedelta(hours=2),
        )
        QuizProgress.objects.create(
            question=q3,
            stage=QuizProgress.Stage.SEVEN_DAYS,
            next_review_at=now + timedelta(days=1),
        )
        QuizProgress.objects.create(
            question=blank,
            stage=QuizProgress.Stage.RESET,
            wrong_count=0,
            next_review_at=None,
            manual_wrong_note_at=None,
        )
        for question in (hidden, archived, stale):
            QuizProgress.objects.create(
                question=question,
                stage=QuizProgress.Stage.ONE_DAY,
                next_review_at=now - timedelta(days=1),
            )

        session = QuizSession.objects.create(
            domain="english",
            difficulty="beginner",
            mode=QuizSession.Mode.NEW,
            required_count=10,
        )
        item = QuizSessionItem.objects.create(
            session=session,
            question=q2,
            position=1,
            answered_at=now - timedelta(minutes=10),
            correct=False,
            accepted_choice_ids=["b"],
        )
        item.feedback_snapshot = {"correct_choice_ids": ["a"], "explanation": "secret"}
        item.save(update_fields=["feedback_snapshot"])

        with patch("dashboard.quiz_review.timezone.now", return_value=now):
            response = self.client.get("/api/quiz/review/?domain=english&difficulty=beginner&due_only=1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["question_id"] for item in payload["items"]], [q2.pk, q1.pk])
        self.assertEqual(payload["due_count"], 2)
        self.assertEqual(payload["stage_counts"], {"1d": 1, "3d": 1})
        self.assertEqual(payload["items"][0]["prior_feedback"]["correct"], False)
        assert_forbidden_absent(self, payload)

        self.assertEqual(
            self.client.get("/api/quiz/review/?domain=bad").status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/api/quiz/review/?due_only=true").status_code,
            400,
        )

        with patch("dashboard.quiz_review.timezone.now", return_value=now):
            response = self.client.get("/api/quiz/review/?domain=english")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            {item["question_id"] for item in payload["items"]},
            {q1.pk, q2.pk, q3.pk},
        )
        self.assertNotIn(blank.pk, {item["question_id"] for item in payload["items"]})
        self.assertEqual(payload["due_count"], 2)
        self.assertEqual(payload["stage_counts"], {"1d": 1, "3d": 1, "7d": 1})

    def test_manual_wrong_note_toggles_without_mutating_question_source(self):
        question = self.create_question("manual")
        original = {
            "source_hash": question.source_hash,
            "source_key": question.knowledge_item.source_key,
        }

        with patch(
            "dashboard.quiz_review.timezone.now",
            return_value=datetime(2026, 7, 21, 12, tzinfo=UTC),
        ):
            response = self.patch_json(
                f"/api/quiz/questions/{question.pk}/wrong-note/",
                {"manual_wrong_note": True, "note": "review later"},
            )
        self.assertEqual(response.status_code, 200)
        progress = QuizProgress.objects.get(question=question)
        self.assertEqual(progress.stage, QuizProgress.Stage.RESET)
        self.assertEqual(progress.wrong_count, 1)
        self.assertEqual(progress.correct_streak, 0)
        self.assertIsNotNone(progress.manual_wrong_note_at)
        self.assertEqual(
            progress.next_review_at,
            datetime(2026, 7, 22, 12, tzinfo=UTC),
        )
        first_manual_wrong_note_at = progress.manual_wrong_note_at
        first_next_review_at = progress.next_review_at

        with patch(
            "dashboard.quiz_review.timezone.now",
            return_value=datetime(2026, 7, 21, 13, tzinfo=UTC),
        ):
            response = self.patch_json(
                f"/api/quiz/questions/{question.pk}/wrong-note/",
                {"manual_wrong_note": True, "note": "still review later"},
            )
        self.assertEqual(response.status_code, 200)
        progress.refresh_from_db()
        self.assertEqual(progress.wrong_count, 1)
        self.assertEqual(progress.manual_wrong_note_at, first_manual_wrong_note_at)
        self.assertEqual(progress.next_review_at, first_next_review_at)

        response = self.patch_json(
            f"/api/quiz/questions/{question.pk}/wrong-note/",
            {"manual_wrong_note": False, "note": "discarded metadata"},
        )
        self.assertEqual(response.status_code, 200)
        progress.refresh_from_db()
        self.assertEqual(progress.wrong_count, 1)
        self.assertEqual(progress.correct_streak, 0)
        self.assertEqual(
            progress.next_review_at,
            datetime(2026, 7, 22, 12, tzinfo=UTC),
        )
        self.assertIsNone(progress.manual_wrong_note_at)

        question.refresh_from_db()
        self.assertEqual(question.source_hash, original["source_hash"])
        self.assertEqual(question.knowledge_item.source_key, original["source_key"])
        self.assertFalse(hasattr(progress, "note"))

    def test_manual_wrong_note_false_without_progress_is_noop(self):
        question = self.create_question("manual-noop")

        response = self.patch_json(
            f"/api/quiz/questions/{question.pk}/wrong-note/",
            {"manual_wrong_note": False},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(QuizProgress.objects.filter(question=question).count(), 0)
        payload = response.json()["progress"]
        self.assertEqual(payload["stage"], QuizProgress.Stage.RESET)
        self.assertEqual(payload["wrong_count"], 0)
        self.assertEqual(payload["correct_streak"], 0)
        self.assertIsNone(payload["next_review_at"])
        self.assertIsNone(payload["manual_wrong_note_at"])

    def test_manual_wrong_note_validation_and_locked_questions(self):
        question = self.create_question("valid")
        hidden = self.create_question("hidden-lock", item=self.create_item("hidden-lock", hidden=True))

        response = self.patch_json(
            f"/api/quiz/questions/{question.pk}/wrong-note/",
            {"manual_wrong_note": "yes"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_note")

        response = self.patch_json(
            f"/api/quiz/questions/{question.pk}/wrong-note/",
            {"manual_wrong_note": True, "note": "x" * 2001},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_note")

        response = self.patch_json(
            f"/api/quiz/questions/{hidden.pk}/wrong-note/",
            {"manual_wrong_note": True},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "quiz_question_locked")

        response = self.patch_json(
            "/api/quiz/questions/999999/wrong-note/",
            {"manual_wrong_note": True},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "quiz_question_not_found")

    def test_review_and_wrong_sessions_prioritize_and_fill_without_duplicates(self):
        questions = self.seed_bank()
        now = datetime(2026, 7, 21, 12, tzinfo=UTC)
        QuizProgress.objects.create(
            question=questions[6],
            stage=QuizProgress.Stage.THREE_DAYS,
            next_review_at=now - timedelta(hours=1),
        )
        QuizProgress.objects.create(
            question=questions[7],
            stage=QuizProgress.Stage.RESET,
            wrong_count=1,
            next_review_at=now + timedelta(days=20),
        )
        QuizProgress.objects.create(
            question=questions[8],
            stage=QuizProgress.Stage.RESET,
            manual_wrong_note_at=now,
            next_review_at=now + timedelta(days=30),
        )

        with patch("dashboard.quiz_sessions.timezone.now", return_value=now):
            review = self.post_json(
                "/api/quiz/sessions/",
                {"domain": "english", "difficulty": "beginner", "mode": "review"},
            ).json()
            wrong = self.post_json(
                "/api/quiz/sessions/",
                {"domain": "english", "difficulty": "beginner", "mode": "wrong"},
            ).json()

        review_ids = list(
            QuizSessionItem.objects.filter(session__session_id=review["session_id"])
            .order_by("position")
            .values_list("question_id", flat=True)
        )
        wrong_ids = list(
            QuizSessionItem.objects.filter(session__session_id=wrong["session_id"])
            .order_by("position")
            .values_list("question_id", flat=True)
        )
        self.assertEqual(review_ids[0], questions[6].pk)
        self.assertEqual(wrong_ids[:2], [questions[7].pk, questions[8].pk])
        self.assertEqual(len(review_ids), 10)
        self.assertEqual(len(wrong_ids), 10)
        self.assertEqual(len(review_ids), len(set(review_ids)))
        self.assertEqual(len(wrong_ids), len(set(wrong_ids)))

    def test_due_review_answer_retry_does_not_double_advance(self):
        questions = self.seed_bank()
        now = datetime(2026, 7, 21, 12, tzinfo=UTC)
        QuizProgress.objects.create(
            question=questions[0],
            stage=QuizProgress.Stage.THREE_DAYS,
            next_review_at=now - timedelta(minutes=1),
        )
        with patch("dashboard.quiz_sessions.timezone.now", return_value=now):
            session = self.post_json(
                "/api/quiz/sessions/",
                {"domain": "english", "difficulty": "beginner", "mode": "review"},
            ).json()
            item_id = session["current_item"]["id"]
            first = self.post_json(
                f"/api/quiz/sessions/{session['session_id']}/items/{item_id}/answer/",
                {"choice_ids": ["a"]},
            ).json()
            second = self.post_json(
                f"/api/quiz/sessions/{session['session_id']}/items/{item_id}/answer/",
                {"choice_ids": ["a"]},
            ).json()

        progress = QuizProgress.objects.get(question=questions[0])
        self.assertEqual(first, second)
        self.assertEqual(progress.stage, QuizProgress.Stage.SEVEN_DAYS)
        self.assertEqual(progress.correct_streak, 1)

    def test_completed_review_result_reports_mastered_count(self):
        questions = self.seed_bank()
        now = datetime(2026, 7, 21, 12, tzinfo=UTC)
        QuizProgress.objects.create(
            question=questions[0],
            stage=QuizProgress.Stage.THIRTY_DAYS,
            next_review_at=now - timedelta(minutes=1),
        )

        with patch("dashboard.quiz_sessions.timezone.now", return_value=now):
            session = self.post_json(
                "/api/quiz/sessions/",
                {"domain": "english", "difficulty": "beginner", "mode": "review"},
            ).json()
            for _index in range(10):
                current = self.client.get(
                    f"/api/quiz/sessions/{session['session_id']}/"
                ).json()["current_item"]
                response = self.post_json(
                    f"/api/quiz/sessions/{session['session_id']}/items/{current['id']}/answer/",
                    {"choice_ids": ["a"]},
                )
                self.assertEqual(response.status_code, 200)

        result = self.client.get(f"/api/quiz/sessions/{session['session_id']}/result/")

        self.assertEqual(result.status_code, 200)
        payload = result.json()
        self.assertEqual(payload["mastered_count"], 1)
        self.assertEqual(payload["correct_count"], 10)
        assert_forbidden_absent(self, payload, FORBIDDEN_RESULT)

    def test_seoul_today_goal_and_streak_use_local_midnight(self):
        question = self.create_question("seoul")
        QuizProgress.objects.create(
            question=question,
            stage=QuizProgress.Stage.RESET,
            next_review_at=datetime(2026, 7, 21, 15, 5, tzinfo=UTC),
        )
        today_session = QuizSession.objects.create(
            domain="english",
            difficulty="beginner",
            mode=QuizSession.Mode.NEW,
            status=QuizSession.Status.COMPLETED,
            completed_at=datetime(2026, 7, 21, 15, 10, tzinfo=UTC),
        )
        duplicate_today_session = QuizSession.objects.create(
            domain="english",
            difficulty="beginner",
            mode=QuizSession.Mode.NEW,
            status=QuizSession.Status.COMPLETED,
            completed_at=datetime(2026, 7, 21, 16, 10, tzinfo=UTC),
        )
        yesterday_session = QuizSession.objects.create(
            domain="english",
            difficulty="beginner",
            mode=QuizSession.Mode.NEW,
            status=QuizSession.Status.COMPLETED,
            completed_at=datetime(2026, 7, 20, 15, 10, tzinfo=UTC),
        )
        QuizSession.objects.create(
            domain="english",
            difficulty="beginner",
            mode=QuizSession.Mode.NEW,
            status=QuizSession.Status.COMPLETED,
            completed_at=datetime(2026, 7, 18, 15, 10, tzinfo=UTC),
        )
        QuizSessionItem.objects.create(
            session=today_session,
            question=question,
            position=1,
            answered_at=datetime(2026, 7, 21, 15, 5, tzinfo=UTC),
            correct=True,
            accepted_choice_ids=["a"],
        )
        QuizSessionItem.objects.create(
            session=yesterday_session,
            question=question,
            position=1,
            answered_at=datetime(2026, 7, 21, 14, 55, tzinfo=UTC),
            correct=True,
            accepted_choice_ids=["a"],
        )

        with patch(
            "dashboard.quiz_review.timezone.now",
            return_value=datetime(2026, 7, 21, 15, 30, tzinfo=UTC),
        ):
            payload = self.client.get("/api/quiz/review/").json()

        self.assertEqual(payload["today_goal"]["completed"], 1)
        self.assertEqual(payload["today_goal"]["remaining"], 9)
        self.assertEqual(payload["streak"]["current_days"], 2)
        self.assertEqual(duplicate_today_session.status, QuizSession.Status.COMPLETED)
