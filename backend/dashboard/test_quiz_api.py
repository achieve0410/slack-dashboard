import json
import hashlib
from datetime import UTC, datetime, timedelta

from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.utils import timezone

from .models import (
    Category,
    ContentRun,
    CronJob,
    KnowledgeConsumptionState,
    KnowledgeItem,
    QuizGenerationBatch,
    QuizDomainConfig,
    QuizProgress,
    QuizQuestion,
    QuizSession,
    QuizSessionItem,
)


NOW = datetime(2026, 7, 21, tzinfo=UTC)
FORBIDDEN_PRE_SUBMIT = {
    "correct_choice_ids",
    "explanation",
    "rationale",
    "evidence_excerpt",
    "evidence_digest",
    "source",
    "detail_url",
    "source_key",
    "source_hash",
}
FORBIDDEN_RESULT = {"evidence_excerpt", "evidence_digest", "source_hash"}


def assert_forbidden_absent(testcase, payload, forbidden=FORBIDDEN_PRE_SUBMIT):
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


class QuizApiTests(TestCase):
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
            external_id=f"quiz-{suffix}",
            name=f"Quiz {suffix}",
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

    def create_slack_item(self, suffix: str):
        return KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key=f"slack:quiz:{suffix}",
            category=self.english,
            status=KnowledgeItem.Status.CLASSIFIED,
            title=f"Slack {suffix}",
            summary="summary",
            question="question",
            answer="answer",
            source_hash=hashlib.sha256(suffix.encode()).hexdigest(),
            generated_at=NOW,
            classified_at=NOW,
        )

    def create_question(
        self,
        suffix: str,
        *,
        domain="english",
        difficulty="beginner",
        question_type="single_choice",
        item=None,
        active=True,
        state=QuizQuestion.PublishState.PUBLISHED,
        source_hash=None,
    ):
        item = item or self.create_item(suffix, category=self.aws if domain == "aws_saa" else self.english)
        choices = [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}]
        correct = ["a"]
        if question_type == "multiple_select":
            choices.append({"id": "c", "text": "C"})
            correct = ["a", "c"]
        return QuizQuestion.objects.create(
            batch=self.batch,
            knowledge_item=item,
            domain=domain,
            difficulty=difficulty,
            question_type=question_type,
            prompt=f"Prompt {suffix}",
            choices=choices,
            correct_choice_ids=correct,
            explanation=f"Explanation {suffix}",
            evidence_excerpt="Body",
            evidence_digest="d" * 64,
            source_hash=source_hash or item.source_hash,
            generator_version="quizgen-v1",
            prompt_version="prompt-v1",
            prompt_digest="e" * 64,
            publish_state=state,
            is_active=active,
            published_at=timezone.now() if state == QuizQuestion.PublishState.PUBLISHED else None,
        )

    def seed_english_bank(self, count=10):
        return [self.create_question(f"e{index}") for index in range(count)]

    def post_json(self, url, payload):
        return self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def answer_url(self, session_id, item_id):
        return f"/api/quiz/sessions/{session_id}/items/{item_id}/answer/"

    def start_session(self, *, domain="english", difficulty="beginner", mode="new"):
        return self.post_json(
            "/api/quiz/sessions/",
            {"domain": domain, "difficulty": difficulty, "mode": mode},
        )

    def test_catalog_filters_bank_and_empty_state(self):
        valid = self.create_question("valid")
        self.create_question("draft", state=QuizQuestion.PublishState.DRAFT, active=False)
        self.create_question("inactive", active=False)
        self.create_question(
            "superseded",
            state=QuizQuestion.PublishState.SUPERSEDED,
            active=False,
        )
        stale_item = self.create_item("stale")
        self.create_question("staleq", item=stale_item, source_hash="f" * 64)
        hidden_item = self.create_item("hidden", hidden=True)
        self.create_question("hiddenq", item=hidden_item)
        archived_item = self.create_item("archived", archived=True)
        self.create_question("archivedq", item=archived_item)
        QuizDomainConfig.objects.create(
            slug="disabled",
            label="Disabled",
            category_path="학습/비활성",
            allowed_question_types=["single_choice"],
            enabled=False,
        )
        self.create_question("disabledq", domain="disabled")

        payload = self.client.get("/api/quiz/catalog/").json()

        self.assertEqual(payload["available_counts"], {"english:beginner": 1})
        self.assertFalse(payload["empty_state"])
        self.assertEqual(valid.domain, "english")

        QuizQuestion.objects.update(is_active=False)
        self.assertTrue(self.client.get("/api/quiz/catalog/").json()["empty_state"])

    def test_shortage_is_409_zero_write_and_secret_free(self):
        self.seed_english_bank(9)
        response = self.start_session()

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertEqual(payload["code"], "quiz_pool_shortage")
        self.assertEqual(payload["available_count"], 9)
        self.assertEqual(QuizSession.objects.count(), 0)
        self.assertEqual(QuizSessionItem.objects.count(), 0)
        assert_forbidden_absent(self, payload)

    def test_session_creation_and_presubmit_secrecy(self):
        questions = self.seed_english_bank(12)
        response = self.start_session()

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        assert_forbidden_absent(self, payload)
        self.assertEqual(payload["available_count"], 12)
        self.assertEqual(payload["current_item"]["position"], 1)
        self.assertEqual(len(payload["items"]), 10)
        self.assertEqual(QuizSessionItem.objects.count(), 10)
        self.assertEqual(
            list(
                QuizSessionItem.objects.order_by("position").values_list(
                    "question_id",
                    flat=True,
                )
            ),
            [question.pk for question in questions[:10]],
        )

    def test_session_history_ordering_limit_counts_and_secrecy(self):
        questions = self.seed_english_bank()
        old = QuizSession.objects.create(
            domain="english",
            difficulty="beginner",
            mode="new",
            started_at=NOW,
        )
        active = QuizSession.objects.create(
            domain="english",
            difficulty="beginner",
            mode="review",
            started_at=NOW + timedelta(hours=1),
        )
        completed = QuizSession.objects.create(
            domain="english",
            difficulty="beginner",
            mode="wrong",
            status=QuizSession.Status.COMPLETED,
            started_at=NOW + timedelta(hours=2),
            completed_at=NOW + timedelta(hours=3),
        )
        QuizSessionItem.objects.bulk_create(
            [
                QuizSessionItem(
                    session=completed,
                    question=questions[0],
                    position=1,
                    answered_at=NOW + timedelta(hours=2, minutes=1),
                    correct=True,
                    accepted_choice_ids=["a"],
                ),
                QuizSessionItem(
                    session=completed,
                    question=questions[1],
                    position=2,
                    answered_at=NOW + timedelta(hours=2, minutes=2),
                    correct=False,
                    accepted_choice_ids=["b"],
                ),
                QuizSessionItem(session=completed, question=questions[2], position=3),
                QuizSessionItem(
                    session=active,
                    question=questions[3],
                    position=1,
                    answered_at=NOW + timedelta(hours=1, minutes=1),
                    correct=False,
                    accepted_choice_ids=["b"],
                ),
                QuizSessionItem(session=active, question=questions[4], position=2),
                QuizSessionItem(session=old, question=questions[5], position=1),
            ]
        )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/quiz/sessions/?limit=2")

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 3)
        payload = response.json()
        assert_forbidden_absent(self, payload)
        self.assertEqual(
            [item["session_id"] for item in payload["results"]],
            [str(completed.session_id), str(active.session_id)],
        )
        self.assertEqual(payload["results"][0]["status"], "completed")
        self.assertEqual(payload["results"][0]["mode"], "wrong")
        self.assertEqual(payload["results"][0]["answered_count"], 2)
        self.assertEqual(payload["results"][0]["total_count"], 3)
        self.assertEqual(payload["results"][0]["score"], 1)
        self.assertEqual(payload["results"][1]["status"], "active")
        self.assertEqual(payload["results"][1]["answered_count"], 1)
        self.assertEqual(payload["results"][1]["total_count"], 2)
        self.assertEqual(payload["results"][1]["score"], 0)

    def test_session_history_invalid_limit(self):
        for value in ("0", "51", "bad", ""):
            with self.subTest(value=value):
                response = self.client.get(f"/api/quiz/sessions/?limit={value}")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "invalid_limit")

    def test_invalid_answers_and_conflicts_do_not_create_progress(self):
        self.seed_english_bank()
        session = self.start_session().json()
        item_id = session["current_item"]["id"]
        invalid_cases = (
            {"choice_ids": []},
            {"choice_ids": ["a", "a"]},
            {"choice_ids": ["missing"]},
            {"choice_ids": [1]},
            {"choice_ids": ["a", "b"]},
        )
        for payload in invalid_cases:
            with self.subTest(payload=payload):
                response = self.post_json(self.answer_url(session["session_id"], item_id), payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "invalid_choice_ids")
        self.assertEqual(QuizProgress.objects.count(), 0)

        accepted = self.post_json(
            self.answer_url(session["session_id"], item_id),
            {"choice_ids": ["a"]},
        )
        self.assertEqual(accepted.status_code, 200)
        before = QuizProgress.objects.get()
        conflict = self.post_json(
            self.answer_url(session["session_id"], item_id),
            {"choice_ids": ["b"]},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "quiz_answer_conflict")
        before.refresh_from_db()
        self.assertEqual(before.correct_streak, 1)
        self.assertEqual(QuizProgress.objects.count(), 1)

    def test_same_retry_preserves_snapshot_and_progress(self):
        self.seed_english_bank()
        session = self.start_session().json()
        item_id = session["current_item"]["id"]
        first = self.post_json(
            self.answer_url(session["session_id"], item_id),
            {"choice_ids": ["a"]},
        ).json()
        progress = QuizProgress.objects.get()
        updated_at = progress.updated_at
        answered_at = QuizSessionItem.objects.get(pk=item_id).answered_at

        second = self.post_json(
            self.answer_url(session["session_id"], item_id),
            {"choice_ids": ["a"]},
        ).json()

        progress.refresh_from_db()
        self.assertEqual(second, first)
        self.assertEqual(progress.updated_at, updated_at)
        self.assertEqual(QuizSessionItem.objects.get(pk=item_id).answered_at, answered_at)

    def test_multiselect_order_independent_and_source_urls(self):
        aws_questions = [
            self.create_question(
                f"a{index}",
                domain="aws_saa",
                question_type="multiple_select",
            )
            for index in range(10)
        ]
        response = self.start_session(domain="aws_saa")
        item_id = response.json()["current_item"]["id"]

        answer = self.post_json(
            self.answer_url(response.json()["session_id"], item_id),
            {"choice_ids": ["c", "a"]},
        ).json()

        self.assertTrue(answer["correct"])
        self.assertEqual(answer["accepted_choice_ids"], ["a", "c"])
        self.assertEqual(answer["source"]["detail_url"], f"/runs/{aws_questions[0].knowledge_item.content_run_id}")

    def test_slack_source_detail_url(self):
        slack_items = [self.create_slack_item(f"s{index}") for index in range(10)]
        for index, item in enumerate(slack_items):
            self.create_question(f"slack{index}", item=item)
        session = self.start_session().json()
        item_id = session["current_item"]["id"]

        answer = self.post_json(
            self.answer_url(session["session_id"], item_id),
            {"choice_ids": ["a"]},
        ).json()

        self.assertEqual(answer["source"]["detail_url"], f"/knowledge/{slack_items[0].pk}")

    def test_auto_finalize_result_and_evidence_secrecy(self):
        self.seed_english_bank()
        session = self.start_session().json()
        session_id = session["session_id"]
        result_pending = self.client.get(f"/api/quiz/sessions/{session_id}/result/")
        self.assertEqual(result_pending.status_code, 409)
        self.assertEqual(result_pending.json()["code"], "quiz_session_incomplete")

        final_answer = None
        for index in range(10):
            current = self.client.get(f"/api/quiz/sessions/{session_id}/").json()["current_item"]
            response = self.post_json(
                self.answer_url(session_id, current["id"]),
                {"choice_ids": ["b" if index == 9 else "a"]},
            )
            self.assertEqual(response.status_code, 200)
            final_answer = response.json()

        self.assertEqual(final_answer["session_summary"]["status"], "completed")
        self.assertEqual(final_answer["session_summary"]["answered_count"], 10)
        self.assertEqual(final_answer["session_summary"]["correct_count"], 9)
        self.assertEqual(final_answer["session_summary"]["incorrect_count"], 1)

        session_payload = self.client.get(f"/api/quiz/sessions/{session_id}/").json()
        self.assertEqual(session_payload["status"], "completed")
        self.assertIsNone(session_payload["current_item"])
        result = self.client.get(f"/api/quiz/sessions/{session_id}/result/")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["correct_count"], 9)
        self.assertEqual(result.json()["incorrect_count"], 1)
        assert_forbidden_absent(self, result.json(), FORBIDDEN_RESULT)

    def test_out_of_sequence_and_hidden_after_session_snapshot_policy(self):
        questions = self.seed_english_bank()
        session = self.start_session().json()
        items = list(QuizSessionItem.objects.order_by("position"))
        out_of_sequence = self.post_json(
            self.answer_url(session["session_id"], items[1].pk),
            {"choice_ids": ["a"]},
        )
        self.assertEqual(out_of_sequence.status_code, 409)
        self.assertEqual(out_of_sequence.json()["code"], "quiz_item_locked")

        first_item = items[0]
        first_item.question.knowledge_item.hidden_at = timezone.now()
        first_item.question.knowledge_item.save(update_fields=["hidden_at"])
        answer = self.post_json(
            self.answer_url(session["session_id"], first_item.pk),
            {"choice_ids": ["a"]},
        ).json()
        self.assertEqual(answer["source"]["detail_url"], f"/runs/{questions[0].knowledge_item.content_run_id}")
        self.assertNotIn("Body", json.dumps(answer))

        shortage = self.start_session()
        self.assertEqual(shortage.status_code, 409)

    def test_fetch_query_count_is_bounded(self):
        self.seed_english_bank()
        session = self.start_session().json()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(f"/api/quiz/sessions/{session['session_id']}/")
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 5)
