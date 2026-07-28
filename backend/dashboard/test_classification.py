import json
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase

from . import llm
from .classification import (
    MODEL,
    PROVIDER,
    ClassificationDecision,
    ClassifierValidationError,
    InferenceResult,
    TransientInferenceError,
    active_category_catalog,
    apply_decision,
    build_prompt,
    invoke_llm,
    mark_invalid_output,
    parse_decision,
)
from .models import Category, ContentRun, CronJob, FreeQuestionMessage, KnowledgeItem
from .admin import approve_knowledge_items
from .services import reconcile_cron_runs, reconcile_slack_thread


FAKE_LLM_CONFIG = llm.LLMConfig(
    provider="anthropic",
    model="test-model",
    api_key="test-key",
    max_tokens=100,
)


def create_category_path(path: str, *, created_by=None) -> Category:
    """Create (or reuse) the full ancestor chain for a category path.

    There is no seed migration anymore (categories are created on demand by
    the classifier), so tests that need a pre-existing category build it
    themselves with this helper.
    """
    created_by = created_by or Category.CreatedBy.SYSTEM
    parent = None
    category = None
    segments: list[str] = []
    for depth, name in enumerate(path.split("/"), start=1):
        segments.append(name)
        display_path = "/".join(segments)
        path_key = Category.canonical_path_key(display_path)
        try:
            category = Category.exact_category(path_key)
        except Category.DoesNotExist:
            category = Category.objects.create(
                name=name,
                path=display_path,
                parent=parent,
                depth=depth,
                created_by=created_by,
                is_active=True,
            )
        parent = category
    return category


def decision_payload(**overrides):
    payload = {
        "title": "분류 제목",
        "summary": "분류 요약",
        "category_id": None,
        "new_category_path": ["학습", "언어", "프랑스어"],
        "confidence": 0.9,
        "reason": "가장 구체적인 분류입니다.",
    }
    payload.update(overrides)
    return payload


class ClassificationFixtureMixin:
    def tearDown(self):
        (Path(settings.BASE_DIR) / "run" / "classify_knowledge.lock").unlink(
            missing_ok=True
        )
        super().tearDown()

    def create_pending_cron(self, suffix="1", body="분류할 본문"):
        job = CronJob.objects.create(
            external_id=f"other-{suffix}",
            name=f"기타 {suffix}",
            category=CronJob.Category.OTHER,
        )
        run = ContentRun.objects.create(
            job=job,
            external_ts=None,
            status=ContentRun.Status.SUCCESS,
            title=f"기타 제목 {suffix}",
            body=body,
            generated_at=datetime(2026, 7, 15, int(suffix), tzinfo=UTC),
        )
        reconcile_cron_runs([run.pk])
        return run.knowledge_item

    def create_pending_slack(self, suffix="1"):
        thread_ts = f"700.{suffix}00"
        question = FreeQuestionMessage.objects.create(
            external_ts=f"700.{suffix}01",
            thread_ts=thread_ts,
            role=FreeQuestionMessage.Role.USER,
            content=f"질문 {suffix}",
            generated_at=datetime(2026, 7, 15, int(suffix), tzinfo=UTC),
        )
        answer = FreeQuestionMessage.objects.create(
            external_ts=f"700.{suffix}02",
            thread_ts=thread_ts,
            role=FreeQuestionMessage.Role.ASSISTANT,
            content=f"답변 {suffix}",
            generated_at=datetime(2026, 7, 15, int(suffix), 1, tzinfo=UTC),
        )
        reconcile_slack_thread(thread_ts)
        question.refresh_from_db()
        answer.refresh_from_db()
        return question.knowledge_item, question, answer


class DecisionValidationTests(TestCase):
    def test_accepts_exact_existing_category_response_at_threshold(self):
        category = create_category_path("학습/언어/영어")
        decision = parse_decision(
            json.dumps(
                decision_payload(
                    category_id=category.pk,
                    new_category_path=[],
                    confidence=0.65,
                ),
                ensure_ascii=False,
            )
        )

        self.assertEqual(decision.category_id, category.pk)
        self.assertEqual(decision.confidence, Decimal("0.65"))

    def test_rejects_non_strict_or_structurally_invalid_responses(self):
        valid = decision_payload()
        cases = {
            "code_fence": f"```json\n{json.dumps(valid)}\n```",
            "extra_prose": f"result: {json.dumps(valid)}",
            "unknown_key": json.dumps({**valid, "extra": True}),
            "both_targets": json.dumps(
                {**valid, "category_id": 1, "new_category_path": ["학습"]}
            ),
            "neither_target": json.dumps(
                {**valid, "category_id": None, "new_category_path": []}
            ),
            "four_levels": json.dumps(
                {**valid, "new_category_path": ["1", "2", "3", "4"]}
            ),
            "long_category_segment": json.dumps(
                {**valid, "new_category_path": ["a" * 101]}
            ),
            "invalid_confidence": json.dumps({**valid, "confidence": 1.01}),
            "nan": json.dumps({**valid, "confidence": float("nan")}),
            "empty_title": json.dumps({**valid, "title": ""}),
            "long_summary": json.dumps({**valid, "summary": "a" * 601}),
            "empty_reason": json.dumps({**valid, "reason": ""}),
        }
        for name, raw in cases.items():
            with self.subTest(name=name), self.assertRaises(ClassifierValidationError):
                parse_decision(raw)

    def test_rejects_materialized_path_over_model_limit(self):
        path_field = Category._meta.get_field("path")
        with patch.object(path_field, "max_length", 250), self.assertRaises(
            ClassifierValidationError
        ) as context:
            parse_decision(
                json.dumps(
                    decision_payload(new_category_path=["a" * 90, "b" * 90, "c" * 90])
                )
            )

        self.assertEqual(context.exception.code, "invalid_path_length")

    def test_rejects_casefold_expanded_canonical_path_over_model_limit(self):
        with self.assertRaises(ClassifierValidationError) as context:
            parse_decision(
                json.dumps(
                    decision_payload(new_category_path=["ß" * 100] * 3),
                    ensure_ascii=False,
                )
            )

        self.assertEqual(context.exception.code, "invalid_path_key_length")


class DecisionApplicationTests(ClassificationFixtureMixin, TestCase):
    def test_reuses_existing_category_and_preserves_sources(self):
        item, question, answer = self.create_pending_slack()
        category = create_category_path("학습/언어/영어")
        decision = parse_decision(
            json.dumps(
                decision_payload(
                    category_id=category.pk,
                    new_category_path=[],
                    confidence=0.65,
                )
            )
        )

        outcome = apply_decision(item.pk, item.source_hash, decision)

        item.refresh_from_db()
        question.refresh_from_db()
        answer.refresh_from_db()
        self.assertEqual(outcome, "classified")
        self.assertEqual(item.category_id, category.pk)
        self.assertEqual(item.status, KnowledgeItem.Status.CLASSIFIED)
        self.assertEqual(item.classification_model, MODEL)
        self.assertEqual(item.classification_confidence, Decimal("0.650"))
        self.assertEqual(item.summary, "")
        self.assertEqual(question.content, "질문 1")
        self.assertEqual(answer.content, "답변 1")

    def test_creates_only_missing_descendant_at_threshold(self):
        item = self.create_pending_cron()
        create_category_path("학습/언어")
        before = Category.objects.count()
        decision = parse_decision(json.dumps(decision_payload(confidence=0.75)))

        outcome = apply_decision(item.pk, item.source_hash, decision)

        item.refresh_from_db()
        self.assertEqual(outcome, "classified")
        self.assertEqual(Category.objects.count(), before + 1)
        self.assertEqual(item.category.path, "학습/언어/프랑스어")
        self.assertEqual(item.category.depth, 3)
        self.assertEqual(item.category.created_by, Category.CreatedBy.AI)

    def test_new_root_threshold_and_low_confidence_review(self):
        accepted = self.create_pending_cron("1")
        rejected = self.create_pending_cron("2")
        before = Category.objects.count()
        accepted_decision = parse_decision(
            json.dumps(decision_payload(new_category_path=["업무"], confidence=0.85))
        )
        rejected_decision = parse_decision(
            json.dumps(decision_payload(new_category_path=["취미"], confidence=0.84))
        )

        self.assertEqual(
            apply_decision(accepted.pk, accepted.source_hash, accepted_decision),
            "classified",
        )
        self.assertEqual(
            apply_decision(rejected.pk, rejected.source_hash, rejected_decision),
            "needs_review",
        )

        rejected.refresh_from_db()
        self.assertEqual(Category.objects.count(), before + 1)
        self.assertTrue(Category.objects.filter(path="업무").exists())
        self.assertFalse(Category.objects.filter(path="취미").exists())
        self.assertEqual(rejected.status, KnowledgeItem.Status.NEEDS_REVIEW)

    def test_existing_path_proposal_reuses_normalized_category(self):
        item = self.create_pending_cron()
        create_category_path("학습/자격증/AWS")
        before = Category.objects.count()
        decision = parse_decision(
            json.dumps(
                decision_payload(
                    new_category_path=[" 학습 ", "자격증", "aws"],
                    confidence=0.65,
                )
            )
        )

        outcome = apply_decision(item.pk, item.source_hash, decision)

        item.refresh_from_db()
        self.assertEqual(outcome, "classified")
        self.assertEqual(Category.objects.count(), before)
        self.assertEqual(item.category.path, "학습/자격증/AWS")

    def test_unknown_or_deactivated_category_requires_review(self):
        item = self.create_pending_cron("1")
        inactive_item = self.create_pending_cron("2")
        category = create_category_path("학습/언어/영어")
        unknown = parse_decision(
            json.dumps(
                decision_payload(
                    category_id=999999,
                    new_category_path=[],
                    confidence=0.9,
                )
            )
        )
        inactive = parse_decision(
            json.dumps(
                decision_payload(
                    category_id=category.pk,
                    new_category_path=[],
                    confidence=0.9,
                )
            )
        )
        category.is_active = False
        category.save(update_fields=["is_active"])

        self.assertEqual(apply_decision(item.pk, item.source_hash, unknown), "needs_review")
        self.assertEqual(
            apply_decision(inactive_item.pk, inactive_item.source_hash, inactive),
            "needs_review",
        )
        item.refresh_from_db()
        inactive_item.refresh_from_db()
        self.assertIsNone(item.category_id)
        self.assertIsNone(inactive_item.category_id)

    def test_inactive_ancestor_excludes_catalog_and_requires_review(self):
        item = self.create_pending_cron()
        learning = create_category_path("학습")
        english = create_category_path("학습/언어/영어")
        learning.is_active = False
        learning.save(update_fields=["is_active"])
        decision = parse_decision(
            json.dumps(
                decision_payload(
                    category_id=english.pk,
                    new_category_path=[],
                    confidence=0.9,
                )
            )
        )

        self.assertNotIn(english.pk, {row["id"] for row in active_category_catalog()})
        self.assertEqual(
            apply_decision(item.pk, item.source_hash, decision),
            "needs_review",
        )

    def test_source_or_status_change_discards_inference_result(self):
        source_changed = self.create_pending_cron("1")
        status_changed = self.create_pending_cron("2")
        category = create_category_path("학습/언어/영어")
        decision = parse_decision(
            json.dumps(
                decision_payload(
                    category_id=category.pk,
                    new_category_path=[],
                    confidence=0.9,
                )
            )
        )
        expected_hash = source_changed.source_hash
        source_changed.source_hash = "f" * 64
        source_changed.save(update_fields=["source_hash"])
        status_changed.status = KnowledgeItem.Status.NEEDS_REVIEW
        status_changed.save(update_fields=["status"])

        self.assertEqual(
            apply_decision(source_changed.pk, expected_hash, decision), "stale"
        )
        self.assertEqual(
            apply_decision(status_changed.pk, status_changed.source_hash, decision),
            "stale",
        )
        source_changed.refresh_from_db()
        status_changed.refresh_from_db()
        self.assertIsNone(source_changed.category_id)
        self.assertIsNone(status_changed.category_id)

    def test_path_race_reuses_winner_and_transaction_failure_rolls_back(self):
        raced_item = self.create_pending_cron("1")
        failed_item = self.create_pending_cron("2")
        raced_decision = parse_decision(
            json.dumps(decision_payload(new_category_path=["경쟁"], confidence=0.9))
        )
        winner = Category.objects.create(
            name="경쟁",
            path="경쟁",
            path_key="경쟁",
            depth=1,
            created_by=Category.CreatedBy.USER,
        )

        self.assertEqual(
            apply_decision(raced_item.pk, raced_item.source_hash, raced_decision),
            "classified",
        )
        raced_item.refresh_from_db()
        self.assertEqual(raced_item.category_id, winner.pk)

        failed_decision = parse_decision(
            json.dumps(decision_payload(new_category_path=["롤백"], confidence=0.9))
        )
        with patch.object(KnowledgeItem, "save", side_effect=RuntimeError("fail")):
            with self.assertRaises(RuntimeError):
                apply_decision(failed_item.pk, failed_item.source_hash, failed_decision)
        failed_item.refresh_from_db()
        self.assertEqual(failed_item.status, KnowledgeItem.Status.PENDING)
        self.assertFalse(Category.objects.filter(path="롤백").exists())

    def test_digest_unique_race_recovers_only_the_exact_winner(self):
        item = self.create_pending_cron()
        winner = Category.objects.create(name="Café", path="Café", depth=1)
        decision = parse_decision(
            json.dumps(decision_payload(new_category_path=["Cafe\u0301"], confidence=0.9))
        )
        exact = {winner.path_key: winner}

        with patch.object(Category, "exact_categories", side_effect=[{}, exact]):
            outcome = apply_decision(item.pk, item.source_hash, decision)

        item.refresh_from_db()
        self.assertEqual(outcome, "classified")
        self.assertEqual(item.category_id, winner.pk)
        self.assertEqual(Category.objects.filter(identity_hash=winner.identity_hash).count(), 1)

    def test_invalid_output_marks_review_without_mutating_source(self):
        item = self.create_pending_cron()
        body = item.content_run.body

        outcome = mark_invalid_output(item.pk, item.source_hash, "invalid_json")

        item.refresh_from_db()
        item.content_run.refresh_from_db()
        self.assertEqual(outcome, "needs_review")
        self.assertEqual(item.status, KnowledgeItem.Status.NEEDS_REVIEW)
        self.assertEqual(item.content_run.body, body)
        self.assertEqual(item.classification_reason, "Classifier review required: invalid_json")


class LLMContractTests(ClassificationFixtureMixin, TestCase):
    def test_inference_transport_failures_are_transient(self):
        item = self.create_pending_cron()
        with patch(
            "dashboard.classification.llm.complete",
            side_effect=llm.LLMTransportError("api_error"),
        ):
            with self.assertRaises(TransientInferenceError):
                invoke_llm(FAKE_LLM_CONFIG, item, active_category_catalog(), 10)

class ClassificationCommandTests(ClassificationFixtureMixin, TestCase):
    def command_patches(self, inference_side_effect):
        return (
            patch(
                "dashboard.llm.resolve_llm_config",
                return_value=FAKE_LLM_CONFIG,
            ),
            patch("dashboard.llm.preflight_llm"),
            patch(
                "dashboard.management.commands.classify_knowledge.invoke_llm",
                side_effect=inference_side_effect,
            ),
        )

    def test_batch_continues_after_invalid_output(self):
        invalid_item = self.create_pending_cron("1")
        valid_item = self.create_pending_cron("2")
        category = create_category_path("학습/언어/영어")
        decision = ClassificationDecision(
            title="valid",
            summary="valid summary",
            category_id=category.pk,
            new_category_path=(),
            confidence=Decimal("0.9"),
            reason="valid reason",
        )
        patches = self.command_patches(
            [
                ClassifierValidationError("invalid_json"),
                InferenceResult(decision=decision, usage={"model": MODEL}),
            ]
        )
        stdout = StringIO()
        with patches[0], patches[1], patches[2]:
            call_command("classify_knowledge", limit=2, stdout=stdout)

        invalid_item.refresh_from_db()
        valid_item.refresh_from_db()
        self.assertEqual(invalid_item.status, KnowledgeItem.Status.NEEDS_REVIEW)
        self.assertEqual(valid_item.status, KnowledgeItem.Status.CLASSIFIED)
        self.assertIn("classified=1", stdout.getvalue())
        self.assertIn("needs_review=1", stdout.getvalue())
        self.assertIn(f"model={MODEL}", stdout.getvalue())
        self.assertIn(f"provider={PROVIDER}", stdout.getvalue())
        self.assertIn("elapsed_ms=", stdout.getvalue())
        self.assertIn("missing_usage=0", stdout.getvalue())

    def test_overlong_category_output_moves_item_to_review_without_retry(self):
        item = self.create_pending_cron()
        patches = self.command_patches(
            ClassifierValidationError("invalid_path_segment_length")
        )
        stderr = StringIO()
        with patches[0], patches[1], patches[2] as invoke:
            call_command(
                "classify_knowledge",
                item_id=item.pk,
                limit=1,
                stderr=stderr,
            )

        item.refresh_from_db()
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(item.status, KnowledgeItem.Status.NEEDS_REVIEW)
        self.assertIn("invalid_path_segment_length", item.classification_reason)
        self.assertIn("exception=ClassifierValidationError", stderr.getvalue())

    def test_casefold_expanded_path_moves_item_to_review_without_retry(self):
        item = self.create_pending_cron()
        patches = self.command_patches(
            ClassifierValidationError("invalid_path_key_length")
        )
        stderr = StringIO()
        with patches[0], patches[1], patches[2] as invoke:
            call_command(
                "classify_knowledge",
                item_id=item.pk,
                limit=1,
                stderr=stderr,
            )

        item.refresh_from_db()
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(item.status, KnowledgeItem.Status.NEEDS_REVIEW)
        self.assertIn("invalid_path_key_length", item.classification_reason)
        self.assertIn("exception=ClassifierValidationError", stderr.getvalue())

    def test_transport_failure_leaves_pending_and_limit_is_bounded(self):
        first = self.create_pending_cron("1")
        second = self.create_pending_cron("2")
        patches = self.command_patches(TransientInferenceError("timeout"))
        with patches[0], patches[1], patches[2] as invoke:
            call_command("classify_knowledge", limit=1, stderr=StringIO())

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(first.status, KnowledgeItem.Status.PENDING)
        self.assertEqual(second.status, KnowledgeItem.Status.PENDING)

    def test_unexpected_apply_failure_keeps_item_pending_and_records_traceback(self):
        item = self.create_pending_cron()
        category = create_category_path("학습/언어/영어")
        result = InferenceResult(
            decision=ClassificationDecision(
                title="safe title",
                summary="safe summary",
                category_id=category.pk,
                new_category_path=(),
                confidence=Decimal("0.9"),
                reason="safe reason",
            ),
            usage={},
        )
        patches = self.command_patches([result])
        stderr = StringIO()
        with patches[0], patches[1], patches[2], patch(
            "dashboard.management.commands.classify_knowledge.apply_decision",
            side_effect=RuntimeError("internal failure"),
        ), self.assertLogs(
            "dashboard.management.commands.classify_knowledge",
            level="ERROR",
        ) as captured:
            call_command(
                "classify_knowledge",
                item_id=item.pk,
                limit=1,
                stderr=stderr,
            )

        item.refresh_from_db()
        self.assertEqual(item.status, KnowledgeItem.Status.PENDING)
        self.assertIsNotNone(captured.records[0].exc_info)
        self.assertIn(f"item={item.pk}", captured.output[0])
        self.assertIn("exception=RuntimeError", stderr.getvalue())
        self.assertNotIn(item.content_run.body, captured.output[0])

    def test_item_id_selects_only_requested_eligible_item(self):
        selected = self.create_pending_cron("1")
        unselected = self.create_pending_cron("2")
        category = create_category_path("학습/언어/영어")
        result = InferenceResult(
            decision=ClassificationDecision(
                title="selected",
                summary="summary",
                category_id=category.pk,
                new_category_path=(),
                confidence=Decimal("0.9"),
                reason="reason",
            ),
            usage={},
        )
        patches = self.command_patches([result])
        stdout = StringIO()
        with patches[0], patches[1], patches[2] as invoke:
            call_command(
                "classify_knowledge",
                item_id=selected.pk,
                limit=10,
                stdout=stdout,
            )

        selected.refresh_from_db()
        unselected.refresh_from_db()
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(selected.status, KnowledgeItem.Status.CLASSIFIED)
        self.assertEqual(unselected.status, KnowledgeItem.Status.PENDING)
        self.assertIn("missing_usage=1", stdout.getvalue())
        self.assertIn("category_existing=1", stdout.getvalue())

    def test_unanswered_and_empty_cron_items_are_not_selected(self):
        pending = self.create_pending_cron("1")
        empty = self.create_pending_cron("2", body="")
        empty.status = KnowledgeItem.Status.PENDING
        empty.save(update_fields=["status"])
        thread_ts = "800.000"
        FreeQuestionMessage.objects.create(
            external_ts="800.100",
            thread_ts=thread_ts,
            role=FreeQuestionMessage.Role.USER,
            content="unanswered",
            generated_at=datetime(2026, 7, 15, tzinfo=UTC),
        )
        reconcile_slack_thread(thread_ts)
        patches = self.command_patches(TransientInferenceError("stop"))
        with patches[0], patches[1], patches[2] as invoke:
            call_command("classify_knowledge", limit=10, stderr=StringIO())

        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(invoke.call_args.args[1].pk, pending.pk)

    def test_lock_contention_skips_without_preflight(self):
        @contextmanager
        def unavailable_lock(path):
            yield False

        stdout = StringIO()
        with patch(
            "dashboard.management.commands.classify_knowledge.classifier_lock",
            side_effect=unavailable_lock,
        ), patch("dashboard.llm.preflight_llm") as preflight:
            call_command("classify_knowledge", stdout=stdout)

        preflight.assert_not_called()
        self.assertIn("이미 실행 중", stdout.getvalue())

    def test_category_growth_logs_each_created_path_once_with_item_confidence(self):
        secret = "full question OPENAI_API_KEY=sk-secret-do-not-log"
        created = self.create_pending_cron("1", body=secret)
        reused = self.create_pending_cron("2", body="reuse body")
        existing = self.create_pending_cron("3", body="existing body")
        existing_category = create_category_path("학습/언어/영어")
        growth_decision = ClassificationDecision(
            title="growth",
            summary="growth",
            category_id=None,
            new_category_path=("관찰 성장", "하위"),
            confidence=Decimal("0.91"),
            reason="growth",
        )
        existing_decision = ClassificationDecision(
            title="existing",
            summary="existing",
            category_id=existing_category.pk,
            new_category_path=(),
            confidence=Decimal("0.92"),
            reason="existing",
        )
        patches = self.command_patches(
            [
                InferenceResult(decision=growth_decision, usage={}),
                InferenceResult(decision=growth_decision, usage={}),
                InferenceResult(decision=existing_decision, usage={}),
            ]
        )
        stdout = StringIO()
        stderr = StringIO()

        with patches[0], patches[1], patches[2]:
            call_command(
                "classify_knowledge",
                limit=3,
                stdout=stdout,
                stderr=stderr,
            )

        output = stdout.getvalue()
        growth_lines = [
            line
            for line in output.splitlines()
            if line.startswith("category_growth ")
        ]
        self.assertEqual(
            growth_lines,
            [
                f"category_growth item={created.pk} confidence=0.91 path=관찰_성장",
                f"category_growth item={created.pk} confidence=0.91 path=관찰_성장/하위",
            ],
        )
        self.assertEqual(output.count("path=관찰_성장\n"), 1)
        self.assertEqual(output.count("path=관찰_성장/하위\n"), 1)
        self.assertNotIn(f"item={reused.pk}", "\n".join(growth_lines))
        self.assertNotIn(f"item={existing.pk}", "\n".join(growth_lines))
        summary = next(
            line for line in output.splitlines() if line.startswith("classification_summary ")
        )
        self.assertNotIn("path=", summary)
        self.assertNotIn("category_growth_paths", output)
        self.assertIn("category_created=2", summary)
        self.assertIn("category_reused=1", summary)
        self.assertIn("category_existing=1", summary)
        self.assertNotIn(secret, output)
        self.assertNotIn(secret, stderr.getvalue())

    def test_category_growth_omits_stale_and_rolled_back_creation(self):
        stale_secret = "full stale question credential=stale-secret"
        rollback_secret = "full rollback answer credential=rollback-secret"
        stale = self.create_pending_cron("1", body=stale_secret)
        rolled_back = self.create_pending_cron("2", body=rollback_secret)
        stale_decision = ClassificationDecision(
            title="stale",
            summary="stale",
            category_id=None,
            new_category_path=("스테일 성장",),
            confidence=Decimal("0.93"),
            reason="stale",
        )
        rollback_decision = ClassificationDecision(
            title="rollback",
            summary="rollback",
            category_id=None,
            new_category_path=("롤백 성장",),
            confidence=Decimal("0.94"),
            reason="rollback",
        )

        def infer(_config, item, *_args):
            if item.pk == stale.pk:
                KnowledgeItem.objects.filter(pk=item.pk).update(source_hash="f" * 64)
                return InferenceResult(decision=stale_decision, usage={})
            return InferenceResult(decision=rollback_decision, usage={})

        patches = self.command_patches(infer)
        stdout = StringIO()
        stderr = StringIO()
        with patches[0], patches[1], patches[2], patch.object(
            KnowledgeItem,
            "save",
            side_effect=RuntimeError("forced rollback"),
        ), self.assertLogs(
            "dashboard.management.commands.classify_knowledge",
            level="ERROR",
        ) as captured:
            call_command(
                "classify_knowledge",
                limit=2,
                stdout=stdout,
                stderr=stderr,
            )

        rolled_back.refresh_from_db()
        growth_lines = [
            line
            for line in stdout.getvalue().splitlines()
            if line.startswith("category_growth ")
        ]
        self.assertEqual(growth_lines, [])
        self.assertNotIn("category_growth_paths", stdout.getvalue())
        summary = next(
            line
            for line in stdout.getvalue().splitlines()
            if line.startswith("classification_summary ")
        )
        self.assertIn("category_created=0", summary)
        self.assertIn("stale=1", summary)
        self.assertIn("transient_failure=1", summary)
        self.assertFalse(
            Category.objects.filter(path__in=("스테일 성장", "롤백 성장")).exists()
        )
        self.assertEqual(rolled_back.status, KnowledgeItem.Status.PENDING)
        combined_logs = "\n".join(
            [stdout.getvalue(), stderr.getvalue(), *captured.output]
        )
        self.assertNotIn(stale_secret, combined_logs)
        self.assertNotIn(rollback_secret, combined_logs)


class InferenceTransactionBoundaryTests(TransactionTestCase):
    def tearDown(self):
        (Path(settings.BASE_DIR) / "run" / "classify_knowledge.lock").unlink(
            missing_ok=True
        )
        super().tearDown()

    def test_inference_sends_prompt_and_runs_outside_transaction(self):
        job = CronJob.objects.create(
            external_id="boundary",
            name="boundary",
            category=CronJob.Category.OTHER,
        )
        run = ContentRun.objects.create(
            job=job,
            status=ContentRun.Status.SUCCESS,
            title="boundary title",
            body="boundary body",
            generated_at=datetime(2026, 7, 15, tzinfo=UTC),
        )
        item = KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.CRON,
            source_key=f"cron:{run.pk}",
            content_run=run,
            status=KnowledgeItem.Status.PENDING,
            title=run.title,
            summary=run.body,
            source_hash="0" * 64,
            generated_at=run.generated_at,
        )
        catalog = []
        response_text = json.dumps(decision_payload(new_category_path=["새 분류"]))
        captured = {}

        def fake_complete(config, prompt, *, timeout, operation):
            self.assertFalse(connection.in_atomic_block)
            captured["config"] = config
            captured["prompt"] = prompt
            captured["timeout"] = timeout
            captured["operation"] = operation
            return llm.LLMResponse(
                text=response_text,
                usage={"model": MODEL, "provider": PROVIDER, "input_tokens": 10},
            )

        with patch("dashboard.classification.llm.complete", side_effect=fake_complete):
            result = invoke_llm(FAKE_LLM_CONFIG, item, catalog, 123)

        prompt = json.loads(captured["prompt"])
        self.assertEqual(prompt["active_categories"], catalog)
        self.assertEqual(prompt["item"]["body"], item.content_run.body)
        self.assertEqual(captured["config"], FAKE_LLM_CONFIG)
        self.assertEqual(captured["timeout"], 123)
        self.assertEqual(captured["operation"], "classify")
        self.assertEqual(result.usage["input_tokens"], 10)

    def test_unusable_profile_is_command_level_failure(self):
        with patch(
            "dashboard.llm.resolve_llm_config",
            return_value=FAKE_LLM_CONFIG,
        ), patch(
            "dashboard.llm.preflight_llm",
            side_effect=llm.LLMConfigError("sdk_not_installed"),
        ):
            with self.assertRaises(CommandError):
                call_command("classify_knowledge")

    def test_rejects_unbounded_limit_before_external_calls(self):
        with patch("dashboard.llm.preflight_llm") as preflight:
            with self.assertRaises(CommandError):
                call_command("classify_knowledge", limit=101)
        preflight.assert_not_called()


class CategoryAssignmentConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.english = create_category_path("학습/언어/영어")
        self.root = self.english.parent.parent

    def _pending_cron_item(self, suffix: str) -> KnowledgeItem:
        job = CronJob.objects.create(
            external_id=f"concurrency-{suffix}",
            name=f"concurrency-{suffix}",
            category=CronJob.Category.OTHER,
        )
        run = ContentRun.objects.create(
            job=job,
            status=ContentRun.Status.SUCCESS,
            title=f"title-{suffix}",
            body=f"body-{suffix}",
            generated_at=datetime(2026, 7, 15, tzinfo=UTC),
        )
        return KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.CRON,
            source_key=f"cron:{run.pk}",
            content_run=run,
            status=KnowledgeItem.Status.PENDING,
            title=run.title,
            summary=run.body,
            source_hash=suffix.ljust(64, "0"),
            generated_at=run.generated_at,
        )

    def _run_after_competing_parent_deactivation(self, operation):
        if connection.vendor != "mysql":
            self.skipTest("MySQL row-lock concurrency regression")

        locked = threading.Event()
        release = threading.Event()
        operation_started = threading.Event()
        results = {}
        errors = []
        connection_ids = {}

        def connection_id(name):
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT CONNECTION_ID()")
                connection_ids[name] = cursor.fetchone()[0]

        def deactivate():
            close_old_connections()
            try:
                connection_id("deactivate")
                with transaction.atomic():
                    root = Category.objects.select_for_update().get(pk=self.root.pk)
                    root.is_active = False
                    root.save(update_fields=["is_active"])
                    locked.set()
                    if not release.wait(10):
                        raise TimeoutError("deactivation release timed out")
            except Exception as error:
                errors.append(error)
                locked.set()
            finally:
                connections["default"].close()

        def assign():
            close_old_connections()
            try:
                connection_id("assign")
                operation_started.set()
                results["value"] = operation()
            except Exception as error:
                errors.append(error)
            finally:
                connections["default"].close()

        deactivation_thread = threading.Thread(target=deactivate)
        assignment_thread = threading.Thread(target=assign)
        deactivation_thread.start()
        self.assertTrue(locked.wait(10))
        assignment_thread.start()
        self.assertTrue(operation_started.wait(10))
        time.sleep(0.2)
        self.assertTrue(assignment_thread.is_alive())
        release.set()
        deactivation_thread.join(10)
        assignment_thread.join(10)

        self.assertFalse(deactivation_thread.is_alive())
        self.assertFalse(assignment_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertNotEqual(connection_ids["deactivate"], connection_ids["assign"])
        return results["value"]

    def test_existing_classifier_assignment_waits_and_revalidates_mysql(self):
        item = self._pending_cron_item("classifier")
        decision = ClassificationDecision(
            title="classified",
            summary="classified",
            category_id=self.english.pk,
            new_category_path=(),
            confidence=Decimal("0.9"),
            reason="existing category",
        )

        outcome = self._run_after_competing_parent_deactivation(
            lambda: apply_decision(item.pk, item.source_hash, decision)
        )

        item.refresh_from_db()
        self.assertEqual(outcome, "needs_review")
        self.assertEqual(item.status, KnowledgeItem.Status.NEEDS_REVIEW)
        self.assertIsNone(item.category_id)

    def test_manual_assignment_waits_and_revalidates_mysql(self):
        user = get_user_model().objects.create_user(username="concurrent-reviewer")
        item = KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key="slack:concurrency:manual",
            status=KnowledgeItem.Status.NEEDS_REVIEW,
            title="manual",
            summary="manual",
            question="question",
            answer="answer",
            source_hash="m" * 64,
            generated_at=datetime(2026, 7, 15, tzinfo=UTC),
        )

        def approve():
            try:
                approve_knowledge_items(
                    [item.pk], self.english.pk, user, "concurrency review"
                )
            except ValidationError:
                return "rejected"
            return "approved"

        outcome = self._run_after_competing_parent_deactivation(approve)

        item.refresh_from_db()
        self.assertEqual(outcome, "rejected")
        self.assertEqual(item.status, KnowledgeItem.Status.NEEDS_REVIEW)
        self.assertIsNone(item.category_id)

    def test_concurrent_new_root_creation_reuses_single_winner_mysql(self):
        if connection.vendor != "mysql":
            self.skipTest("MySQL unique-key category creation concurrency regression")

        first = self._pending_cron_item("new-root-first")
        second = self._pending_cron_item("new-root-second")
        items = (first, second)
        path = "동시 신규 루트"
        path_key = Category.canonical_path_key(path)
        decision = ClassificationDecision(
            title="concurrent",
            summary="concurrent",
            category_id=None,
            new_category_path=(path,),
            confidence=Decimal("0.95"),
            reason="concurrent new root",
        )
        source_rows = list(
            KnowledgeItem.objects.filter(pk__in=[item.pk for item in items])
            .order_by("pk")
            .values("pk", "source_key", "source_hash", "content_run_id")
        )
        run_rows = list(
            ContentRun.objects.filter(pk__in=[item.content_run_id for item in items])
            .order_by("pk")
            .values("pk", "job_id", "status", "title", "body", "raw_text", "generated_at")
        )
        job_rows = list(
            CronJob.objects.filter(pk__in=[item.content_run.job_id for item in items])
            .order_by("pk")
            .values("pk", "external_id", "name", "category", "prompt")
        )
        category_count = Category.objects.count()
        start_barrier = threading.Barrier(3, timeout=10)
        missing_barrier = threading.Barrier(2, timeout=10)
        thread_state = threading.local()
        connection_ids = {}
        initial_lookups = []
        outcomes = {}
        errors = []
        original_exact_categories = Category.exact_categories

        def synchronized_exact_categories(path_keys, *, for_update=False):
            result = original_exact_categories(path_keys, for_update=for_update)
            if (
                not for_update
                and tuple(path_keys) == (path_key,)
                and not getattr(thread_state, "initial_lookup_complete", False)
            ):
                thread_state.initial_lookup_complete = True
                initial_lookups.append((threading.current_thread().name, tuple(result)))
                missing_barrier.wait()
            return result

        def classify(name, item):
            close_old_connections()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT CONNECTION_ID()")
                    connection_ids[name] = cursor.fetchone()[0]
                start_barrier.wait()
                outcomes[name] = apply_decision(
                    item.pk,
                    item.source_hash,
                    decision,
                )
            except Exception as error:
                errors.append((name, error))
            finally:
                connections["default"].close()

        threads = [
            threading.Thread(target=classify, args=("first", first), name="new-root-first"),
            threading.Thread(target=classify, args=("second", second), name="new-root-second"),
        ]
        with patch.object(
            Category,
            "exact_categories",
            side_effect=synchronized_exact_categories,
        ):
            for thread in threads:
                thread.start()
            try:
                start_barrier.wait()
            except threading.BrokenBarrierError as error:
                errors.append(("main_start_barrier", error))
            for thread in threads:
                thread.join(20)

        for thread in threads:
            self.assertFalse(thread.is_alive(), f"{thread.name} did not finish")
        self.assertFalse(errors, repr(errors))
        self.assertEqual(set(connection_ids), {"first", "second"})
        self.assertEqual(len(set(connection_ids.values())), 2)
        self.assertEqual(len(initial_lookups), 2)
        self.assertTrue(all(not result for _, result in initial_lookups))
        self.assertEqual(outcomes, {"first": "classified", "second": "classified"})

        category = Category.exact_category(path_key)
        self.assertEqual(Category.objects.count(), category_count + 1)
        self.assertEqual(Category.objects.filter(identity_hash=category.identity_hash).count(), 1)
        self.assertEqual(category.path, path)
        self.assertEqual(category.created_by, Category.CreatedBy.AI)
        for item in items:
            item.refresh_from_db()
            self.assertEqual(item.status, KnowledgeItem.Status.CLASSIFIED)
            self.assertEqual(item.category_id, category.pk)
        self.assertEqual(
            list(
                KnowledgeItem.objects.filter(pk__in=[item.pk for item in items])
                .order_by("pk")
                .values("pk", "source_key", "source_hash", "content_run_id")
            ),
            source_rows,
        )
        self.assertEqual(
            list(
                ContentRun.objects.filter(pk__in=[item.content_run_id for item in items])
                .order_by("pk")
                .values("pk", "job_id", "status", "title", "body", "raw_text", "generated_at")
            ),
            run_rows,
        )
        self.assertEqual(
            list(
                CronJob.objects.filter(pk__in=[item.content_run.job_id for item in items])
                .order_by("pk")
                .values("pk", "external_id", "name", "category", "prompt")
            ),
            job_rows,
        )
