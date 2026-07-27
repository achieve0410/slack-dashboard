import json
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from . import llm
from .models import (
    Category,
    ContentRun,
    CronJob,
    KnowledgeConsumptionState,
    KnowledgeItem,
    OperationRun,
    QuizGenerationBatch,
    QuizQuestion,
)
from .quiz_generation import (
    MODEL,
    PROVIDER,
    GeneratedQuestion,
    GenerationResult,
    MAX_OUTPUT_BYTES,
    QuizGenerationTransientError,
    QuizGenerationValidationError,
    SourcePayload,
    build_prompt,
    generate_with_retries,
    invoke_llm,
    parse_generation,
    publish_questions,
    run_generation_batch,
    source_payload,
)


NOW = datetime(2026, 7, 21, tzinfo=UTC)
FAKE_LLM_CONFIG = llm.LLMConfig(
    provider="anthropic",
    model="test-model",
    api_key="test-key",
    max_tokens=100,
)


class QuizPromptTests(SimpleTestCase):
    def test_evidence_instruction_requires_verbatim_source_excerpt(self):
        source = SourcePayload(
            item=None,
            domain="english",
            source_key="test:1",
            source_hash="a" * 64,
            title="Title",
            text="Exact source sentence.",
        )

        prompt = json.loads(build_prompt(source))

        self.assertIn("copy a short, contiguous passage verbatim", prompt["instruction"])
        self.assertIn("do not paraphrase, translate", prompt["instruction"])


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


def valid_payload(*, domain="english", question_type="single_choice", prompt="Meaning?"):
    return {
        "questions": [
            {
                "domain": domain,
                "difficulty": "beginner",
                "question_type": question_type,
                "prompt": prompt,
                "choices": [
                    {"id": "a", "text": "Correct"},
                    {"id": "b", "text": "Wrong"},
                ],
                "correct_choice_ids": ["a"],
                "explanation": "The source says Correct.",
                "evidence_excerpt": "source says Correct",
            }
        ]
    }


class QuizGenerationParserTests(SimpleTestCase):
    def setUp(self):
        self.source = SourcePayload(
            item=None,
            domain="english",
            source_key="cron:1",
            source_hash="a" * 64,
            title="Source",
            text="The source says Correct in this paragraph.",
        )

    def parse(self, payload):
        return parse_generation(json.dumps(payload), self.source)

    def test_strict_schema_and_grounding(self):
        question = self.parse(valid_payload())[0]
        self.assertEqual(question.domain, "english")
        self.assertEqual(len(question.evidence_digest), 64)

        invalid_outputs = (
            "```json\n{}\n```",
            json.dumps({**valid_payload(), "extra": True}),
            '{"questions":[]}',
            '{"questions":[{"domain":"english","domain":"english"}]}',
            '{"questions":[{"domain":NaN}]}',
            "x" * 70000,
            json.dumps({"questions": [valid_payload()["questions"][0]] * 11}),
        )
        for raw in invalid_outputs:
            with self.subTest(raw=raw[:20]), self.assertRaises(QuizGenerationValidationError):
                parse_generation(raw, self.source)

    def test_rejects_invalid_choice_type_and_ungrounded_evidence(self):
        invalid_choice = valid_payload()
        invalid_choice["questions"][0]["choices"][0]["id"] = 1
        with self.assertRaises(QuizGenerationValidationError):
            self.parse(invalid_choice)

        ungrounded = valid_payload()
        ungrounded["questions"][0]["evidence_excerpt"] = "not in the source"
        with self.assertRaises(QuizGenerationValidationError):
            self.parse(ungrounded)

        multiple_select = valid_payload(domain="english", question_type="multiple_select")
        multiple_select["questions"][0]["correct_choice_ids"] = ["a", "b"]
        with self.assertRaises(QuizGenerationValidationError):
            self.parse(multiple_select)


class QuizGenerationBatchTests(TestCase):
    def setUp(self):
        self.english = create_category_path("학습/언어/영어")
        self.aws = create_category_path("학습/자격증/AWS")

    def create_cron_item(
        self,
        *,
        external_id: str,
        category: Category | None = None,
        source_hash: str = "a" * 64,
        body: str = "The source says Correct in this paragraph.",
        hidden=False,
        archived=False,
    ) -> KnowledgeItem:
        category = category or self.english
        job = CronJob.objects.create(
            external_id=external_id,
            name=external_id,
            category=CronJob.Category.OTHER,
        )
        run = ContentRun.objects.create(
            job=job,
            status=ContentRun.Status.SUCCESS,
            title=f"{external_id} title",
            body=body,
            generated_at=NOW,
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
            hidden_at=timezone.now() if hidden else None,
        )
        if archived:
            KnowledgeConsumptionState.objects.create(
                knowledge_item=item,
                archived_at=timezone.now(),
            )
        return item

    def result(self, source, *, prompt="Meaning?", question_type="single_choice"):
        payload = valid_payload(
            domain=source.domain,
            question_type=question_type,
            prompt=prompt,
        )
        if question_type == "multiple_select":
            payload["questions"][0]["choices"].append({"id": "c", "text": "Also correct"})
            payload["questions"][0]["correct_choice_ids"] = ["a", "c"]
        return GenerationResult(parse_generation(json.dumps(payload), source), {})

    def invoker_with(self, *results):
        calls = {"count": 0}

        def invoker(config, source, timeout):
            result = results[calls["count"]]
            calls["count"] += 1
            if isinstance(result, Exception):
                raise result
            return result(source)

        invoker.calls = calls
        return invoker

    def candidate_for(self, item: KnowledgeItem, domain="english"):
        return type(
            "Candidate",
            (),
            {
                "knowledge_item_id": item.pk,
                "domain": domain,
                "source_key": item.source_key,
                "source_hash": item.source_hash,
                "title": item.title,
            },
        )()

    def test_source_payload_deleted_source_uses_stable_validation_error(self):
        item = self.create_cron_item(external_id="deleted-source", source_hash="a" * 64)
        candidate = self.candidate_for(item)
        item.delete()

        with self.assertRaises(QuizGenerationValidationError) as context:
            source_payload(candidate)

        self.assertEqual(context.exception.code, "source_deleted")

    def test_oversized_response_is_bounded_before_parsing(self):
        item = self.create_cron_item(external_id="transport", source_hash="a" * 64)
        source = source_payload(self.candidate_for(item))

        oversized = llm.LLMResponse(text="x" * (MAX_OUTPUT_BYTES + 1), usage={})
        with patch("dashboard.quiz_generation.llm.complete", return_value=oversized):
            with self.assertRaises(QuizGenerationValidationError) as context:
                invoke_llm(FAKE_LLM_CONFIG, source, 10)
        self.assertEqual(context.exception.code, "output_too_large")

    def test_transport_error_from_llm_is_transient(self):
        item = self.create_cron_item(external_id="transport-error", source_hash="b" * 64)
        source = source_payload(self.candidate_for(item))

        with patch(
            "dashboard.quiz_generation.llm.complete",
            side_effect=llm.LLMTransportError("api_error"),
        ):
            with self.assertRaises(QuizGenerationTransientError) as context:
                invoke_llm(FAKE_LLM_CONFIG, source, 10)
        self.assertEqual(context.exception.code, "api_error")

    def test_bounded_retry_and_failure_isolation(self):
        first = self.create_cron_item(external_id="first", source_hash="a" * 64)
        second = self.create_cron_item(external_id="second", source_hash="b" * 64)

        def fail(_source):
            raise QuizGenerationValidationError("invalid_json")

        invoker = self.invoker_with(fail, fail, lambda source: self.result(source))
        summary = run_generation_batch(
            config=FAKE_LLM_CONFIG,
            timeout=10,
            dry_run=False,
            limit=2,
            max_attempts=2,
            invoker=invoker,
        )

        self.assertEqual(invoker.calls["count"], 3)
        self.assertEqual(summary["quiz_failed"], 1)
        self.assertEqual(summary["quiz_published"], 1)
        self.assertFalse(QuizQuestion.objects.filter(knowledge_item=first).exists())
        self.assertTrue(QuizQuestion.objects.filter(knowledge_item=second).exists())

    def test_dry_run_persists_manifest_but_no_questions_or_supersession(self):
        item = self.create_cron_item(external_id="dry", source_hash="a" * 64)
        source = source_payload(
            self.candidate_for(item)
        )
        old_batch = QuizGenerationBatch.objects.create(
            inventory_version="old",
            dry_run=False,
            status=QuizGenerationBatch.Status.SUCCESS,
            generator_version="old",
        )
        old_question = QuizQuestion.objects.create(
            batch=old_batch,
            knowledge_item=item,
            domain="english",
            difficulty="beginner",
            question_type="single_choice",
            prompt="Old?",
            choices=[{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
            correct_choice_ids=["a"],
            explanation="Old",
            evidence_excerpt="source says Correct",
            evidence_digest="c" * 64,
            source_hash="e" * 64,
            generator_version="old",
            prompt_version="old",
            prompt_digest="d" * 64,
            publish_state=QuizQuestion.PublishState.PUBLISHED,
            is_active=True,
            published_at=timezone.now(),
        )
        summary = run_generation_batch(
            config=FAKE_LLM_CONFIG,
            timeout=10,
            dry_run=True,
            limit=1,
            invoker=self.invoker_with(lambda _source: self.result(source)),
        )

        old_question.refresh_from_db()
        self.assertEqual(summary["quiz_published"], 0)
        self.assertEqual(QuizQuestion.objects.count(), 1)
        self.assertTrue(old_question.is_active)
        self.assertEqual(QuizGenerationBatch.objects.latest("id").dry_run, True)

    def test_publish_duplicates_multiple_questions_and_stale_supersession(self):
        item = self.create_cron_item(external_id="publish", source_hash="a" * 64)
        source = source_payload(
            self.candidate_for(item)
        )
        batch = QuizGenerationBatch.objects.create(
            inventory_version="v1",
            dry_run=False,
            status=QuizGenerationBatch.Status.WRITING,
            generator_version="quizgen-v1",
        )
        stale = QuizQuestion.objects.create(
            batch=batch,
            knowledge_item=item,
            domain="english",
            difficulty="beginner",
            question_type="single_choice",
            prompt="Stale?",
            choices=[{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
            correct_choice_ids=["a"],
            explanation="Old",
            evidence_excerpt="source says Correct",
            evidence_digest="c" * 64,
            source_hash="e" * 64,
            generator_version="old",
            prompt_version="old",
            prompt_digest="d" * 64,
            publish_state=QuizQuestion.PublishState.PUBLISHED,
            is_active=True,
            published_at=timezone.now(),
        )
        first = self.result(source, prompt="First?").questions[0]
        second = self.result(source, prompt="Second?").questions[0]

        published, skipped = publish_questions(batch, source, (first, second, first))

        stale.refresh_from_db()
        self.assertEqual((published, skipped), (2, 1))
        self.assertFalse(stale.is_active)
        self.assertEqual(stale.publish_state, QuizQuestion.PublishState.SUPERSEDED)
        self.assertEqual(
            QuizQuestion.objects.filter(knowledge_item=item, is_active=True).count(),
            2,
        )

    def test_source_hash_and_visibility_race_rechecked_at_publish(self):
        item = self.create_cron_item(external_id="race", source_hash="a" * 64)
        source = source_payload(
            self.candidate_for(item)
        )
        item.source_hash = "b" * 64
        item.save(update_fields=["source_hash"])
        batch = QuizGenerationBatch.objects.create(
            inventory_version="v1",
            dry_run=False,
            status=QuizGenerationBatch.Status.WRITING,
            generator_version="quizgen-v1",
        )

        with self.assertRaises(QuizGenerationValidationError):
            publish_questions(batch, source, self.result(source).questions)
        self.assertFalse(QuizQuestion.objects.exists())

    def test_deleted_source_at_publish_fails_candidate_and_later_candidate_continues(self):
        first = self.create_cron_item(external_id="delete-race", source_hash="a" * 64)
        second = self.create_cron_item(external_id="delete-race-next", source_hash="b" * 64)

        def invoker(config, source, timeout):
            result = self.result(source)
            if source.item.pk == first.pk:
                source.item.delete()
            return result

        summary = run_generation_batch(
            config=FAKE_LLM_CONFIG,
            timeout=10,
            dry_run=False,
            limit=2,
            invoker=invoker,
        )
        outcomes = QuizGenerationBatch.objects.latest("id").candidate_outcomes

        self.assertEqual(summary["quiz_failed"], 1)
        self.assertEqual(summary["quiz_published"], 1)
        self.assertFalse(QuizQuestion.objects.filter(knowledge_item_id=first.pk).exists())
        self.assertTrue(QuizQuestion.objects.filter(knowledge_item=second).exists())
        self.assertIn("source_deleted", [outcome["error_code"] for outcome in outcomes])

    def test_aws_allowlist_and_quarantine_feed_manifest(self):
        allowed = self.create_cron_item(
            external_id="aws-saa",
            category=self.aws,
            source_hash="a" * 64,
        )
        blocked = self.create_cron_item(
            external_id="aws-mixed",
            category=self.aws,
            source_hash="b" * 64,
        )

        summary = run_generation_batch(
            config=FAKE_LLM_CONFIG,
            timeout=10,
            dry_run=False,
            limit=2,
            aws_allowlisted_external_ids=["aws-saa"],
            invoker=self.invoker_with(
                lambda source: self.result(source, question_type="multiple_select")
            ),
        )
        batch = QuizGenerationBatch.objects.latest("id")

        self.assertEqual(summary["quiz_candidates"], 1)
        self.assertEqual(summary["quiz_quarantined"], 1)
        self.assertTrue(QuizQuestion.objects.filter(knowledge_item=allowed).exists())
        self.assertFalse(QuizQuestion.objects.filter(knowledge_item=blocked).exists())
        self.assertEqual(batch.allowlist_snapshot["aws_external_ids"], ["aws-saa"])
        self.assertNotIn("Correct", json.dumps(batch.candidate_outcomes))

    def test_command_operation_summary_is_aggregate_and_secret_free(self):
        def fake_run_generation_batch(**_kwargs):
            return {
                "quiz_candidates": 1,
                "quiz_published": 1,
                "quiz_quarantined": 0,
                "quiz_failed": 0,
                "quiz_dry_run": False,
            }

        with (
            patch("dashboard.llm.resolve_llm_config", return_value=FAKE_LLM_CONFIG),
            patch("dashboard.llm.preflight_llm"),
            patch("dashboard.management.commands.generate_quiz_questions.run_generation_batch", fake_run_generation_batch),
        ):
            call_command("generate_quiz_questions", "--publish", "--limit=1")

        run = OperationRun.objects.latest("id")
        self.assertEqual(run.kind, "quiz")
        self.assertEqual(run.status, "success")
        self.assertEqual(run.summary["quiz_published"], 1)
        self.assertNotIn("candidate_outcomes", run.summary)
        self.assertNotIn("Correct", json.dumps(run.summary))

    def test_lock_contention_skips_before_preflight(self):
        @contextmanager
        def contended(_path):
            yield False

        with (
            patch("dashboard.management.commands.generate_quiz_questions.quiz_generation_lock", contended),
            patch("dashboard.llm.preflight_llm") as preflight,
        ):
            call_command("generate_quiz_questions")

        self.assertFalse(preflight.called)
        run = OperationRun.objects.latest("id")
        self.assertEqual(run.status, "skipped")
        self.assertEqual(run.error_code, "lock_contended")

    def test_inventory_only_skips_llm_resolution_and_preflight(self):
        with (
            patch("dashboard.llm.resolve_llm_config") as resolve,
            patch("dashboard.llm.preflight_llm") as preflight,
            patch(
                "dashboard.management.commands.generate_quiz_questions.run_generation_batch",
                return_value={
                    "quiz_candidates": 0,
                    "quiz_published": 0,
                    "quiz_quarantined": 0,
                    "quiz_failed": 0,
                    "quiz_dry_run": True,
                },
            ),
        ):
            call_command("generate_quiz_questions", "--inventory-only")

        self.assertFalse(resolve.called)
        self.assertFalse(preflight.called)
        run = OperationRun.objects.latest("id")
        self.assertEqual(run.kind, "quiz")
        self.assertEqual(run.status, "success")

    def test_explicit_dry_run_invocation_and_publish_conflict(self):
        captured = {}

        def fake_run_generation_batch(**kwargs):
            captured.update(kwargs)
            return {
                "quiz_candidates": 0,
                "quiz_published": 0,
                "quiz_quarantined": 0,
                "quiz_failed": 0,
                "quiz_dry_run": kwargs["dry_run"],
            }

        with (
            patch("dashboard.llm.resolve_llm_config", return_value=FAKE_LLM_CONFIG),
            patch("dashboard.llm.preflight_llm"),
            patch(
                "dashboard.management.commands.generate_quiz_questions.run_generation_batch",
                fake_run_generation_batch,
            ),
        ):
            call_command("generate_quiz_questions", "--dry-run", "--limit=1")

        self.assertTrue(captured["dry_run"])
        with self.assertRaises(CommandError):
            call_command("generate_quiz_questions", "--dry-run", "--publish")
