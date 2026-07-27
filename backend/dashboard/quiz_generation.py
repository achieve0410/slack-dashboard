import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import llm
from .models import ContentRun, KnowledgeItem, QuizGenerationBatch, QuizQuestion
from .quiz_inventory import QuizInventoryCandidate, collect_quiz_inventory


MODEL = llm.configured_model_name()
PROVIDER = llm.configured_provider_name()
GENERATOR_VERSION = "quizgen-v1"
PROMPT_VERSION = "quiz-prompt-v2"
MAX_BATCH_SIZE = 100
MAX_ATTEMPTS = 2
MAX_OUTPUT_BYTES = 65536
MAX_QUESTIONS_PER_SOURCE = 10
EXPECTED_RESPONSE_KEYS = {"questions"}
EXPECTED_QUESTION_KEYS = {
    "domain",
    "difficulty",
    "question_type",
    "prompt",
    "choices",
    "correct_choice_ids",
    "explanation",
    "evidence_excerpt",
}
ALLOWED_DIFFICULTIES = {
    QuizQuestion.Difficulty.BEGINNER,
    QuizQuestion.Difficulty.INTERMEDIATE,
    QuizQuestion.Difficulty.ADVANCED,
}
ALLOWED_TYPES_BY_DOMAIN = {
    QuizQuestion.Domain.ENGLISH: {QuizQuestion.QuestionType.SINGLE_CHOICE},
    QuizQuestion.Domain.JAPANESE: {QuizQuestion.QuestionType.SINGLE_CHOICE},
    QuizQuestion.Domain.AWS_SAA: {
        QuizQuestion.QuestionType.SINGLE_CHOICE,
        QuizQuestion.QuestionType.MULTIPLE_SELECT,
    },
}


class QuizGenerationValidationError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class QuizGenerationTransientError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SourcePayload:
    item: KnowledgeItem
    domain: str
    source_key: str
    source_hash: str
    title: str
    text: str


@dataclass(frozen=True)
class GeneratedQuestion:
    domain: str
    difficulty: str
    question_type: str
    prompt: str
    choices: list[dict[str, str]]
    correct_choice_ids: list[str]
    explanation: str
    evidence_excerpt: str
    evidence_digest: str


@dataclass(frozen=True)
class GenerationResult:
    questions: tuple[GeneratedQuestion, ...]
    usage: dict


@contextmanager
def quiz_generation_lock(path: Path) -> Iterator[bool]:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def source_payload(candidate: QuizInventoryCandidate) -> SourcePayload:
    try:
        item = (
            KnowledgeItem.objects.select_related("content_run", "content_run__job", "category")
            .get(pk=candidate.knowledge_item_id)
        )
    except KnowledgeItem.DoesNotExist as error:
        raise QuizGenerationValidationError("source_deleted") from error
    text = source_text(item)
    if not text:
        raise QuizGenerationValidationError("source_unavailable")
    return SourcePayload(
        item=item,
        domain=candidate.domain,
        source_key=candidate.source_key,
        source_hash=candidate.source_hash,
        title=candidate.title,
        text=text,
    )


def source_text(item: KnowledgeItem) -> str:
    try:
        archived_at = item.consumption_state.archived_at
    except KnowledgeItem.consumption_state.RelatedObjectDoesNotExist:
        archived_at = None
    if (
        item.status != KnowledgeItem.Status.CLASSIFIED
        or item.hidden_at is not None
        or archived_at is not None
    ):
        return ""
    if item.source_type == KnowledgeItem.SourceType.CRON:
        run = item.content_run
        if (
            run is None
            or run.status != ContentRun.Status.SUCCESS
            or run.hidden_at is not None
        ):
            return ""
        return (run.body or run.raw_text or "").strip()
    if item.source_type == KnowledgeItem.SourceType.SLACK_QA:
        question = item.question.strip()
        answer = item.answer.strip()
        return f"{question}\n\n{answer}".strip() if question and answer else ""
    return ""


def build_prompt(source: SourcePayload) -> str:
    payload = {
        "instruction": (
            "Generate one or more quiz questions from only the provided source. "
            "For every evidence_excerpt, copy a short, contiguous passage verbatim from "
            "source.text; do not paraphrase, translate, or alter punctuation or markdown, "
            "and verify the copied passage occurs in source.text before responding. "
            "Return exactly one JSON object with no markdown or extra prose."
        ),
        "source": {
            "domain": source.domain,
            "title": source.title,
            "text": source.text,
        },
        "response_contract": {
            "questions": [
                {
                    "domain": source.domain,
                    "difficulty": "beginner|intermediate|advanced",
                    "question_type": "single_choice, or multiple_select only for aws_saa",
                    "prompt": "non-empty string",
                    "choices": [{"id": "stable string", "text": "non-empty string"}],
                    "correct_choice_ids": ["choice id strings"],
                    "explanation": "non-empty string",
                    "evidence_excerpt": "non-empty exact excerpt from source text",
                }
            ]
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise QuizGenerationValidationError("duplicate_key")
        result[key] = value
    return result


def _invalid_constant(value: str):
    raise QuizGenerationValidationError("invalid_number")


def parse_generation(raw_output: str, source: SourcePayload) -> tuple[GeneratedQuestion, ...]:
    if len(raw_output.encode()) > MAX_OUTPUT_BYTES:
        raise QuizGenerationValidationError("output_too_large")
    try:
        payload = json.loads(
            raw_output,
            parse_constant=_invalid_constant,
            object_pairs_hook=_strict_object,
        )
    except QuizGenerationValidationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise QuizGenerationValidationError("invalid_json") from error
    if not isinstance(payload, dict) or set(payload) != EXPECTED_RESPONSE_KEYS:
        raise QuizGenerationValidationError("invalid_schema")
    questions = payload["questions"]
    if not isinstance(questions, list) or not questions:
        raise QuizGenerationValidationError("invalid_questions")
    if len(questions) > MAX_QUESTIONS_PER_SOURCE:
        raise QuizGenerationValidationError("too_many_questions")
    return tuple(_parse_question(question, source) for question in questions)


def _parse_question(raw_question: dict, source: SourcePayload) -> GeneratedQuestion:
    if not isinstance(raw_question, dict) or set(raw_question) != EXPECTED_QUESTION_KEYS:
        raise QuizGenerationValidationError("invalid_question_schema")
    domain = raw_question["domain"]
    difficulty = raw_question["difficulty"]
    question_type = raw_question["question_type"]
    if domain != source.domain or domain not in ALLOWED_TYPES_BY_DOMAIN:
        raise QuizGenerationValidationError("invalid_domain")
    if difficulty not in ALLOWED_DIFFICULTIES:
        raise QuizGenerationValidationError("invalid_difficulty")
    if question_type not in ALLOWED_TYPES_BY_DOMAIN[domain]:
        raise QuizGenerationValidationError("invalid_question_type")
    prompt = _non_empty_string(raw_question["prompt"], "invalid_prompt")
    explanation = _non_empty_string(raw_question["explanation"], "invalid_explanation")
    evidence_excerpt = _non_empty_string(
        raw_question["evidence_excerpt"],
        "invalid_evidence",
    )
    if _canonical(evidence_excerpt) not in _canonical(source.text):
        raise QuizGenerationValidationError("ungrounded_evidence")
    choices = _parse_choices(raw_question["choices"])
    correct_choice_ids = _parse_correct_choice_ids(raw_question["correct_choice_ids"])
    evidence_digest = hashlib.sha256(_canonical(evidence_excerpt).encode()).hexdigest()
    generated = GeneratedQuestion(
        domain=domain,
        difficulty=difficulty,
        question_type=question_type,
        prompt=prompt,
        choices=choices,
        correct_choice_ids=correct_choice_ids,
        explanation=explanation,
        evidence_excerpt=evidence_excerpt,
        evidence_digest=evidence_digest,
    )
    if source.item is not None:
        _validate_question_model(generated, source, is_active=False)
    return generated


def _non_empty_string(value, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuizGenerationValidationError(code)
    return value.strip()


def _parse_choices(value) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) < 2:
        raise QuizGenerationValidationError("invalid_choices")
    choices = []
    for choice in value:
        if not isinstance(choice, dict) or set(choice) != {"id", "text"}:
            raise QuizGenerationValidationError("invalid_choices")
        choices.append(
            {
                "id": _non_empty_string(choice["id"], "invalid_choices"),
                "text": _non_empty_string(choice["text"], "invalid_choices"),
            }
        )
    return choices


def _parse_correct_choice_ids(value) -> list[str]:
    if not isinstance(value, list):
        raise QuizGenerationValidationError("invalid_correct_choice_ids")
    return [
        _non_empty_string(choice_id, "invalid_correct_choice_ids")
        for choice_id in value
    ]


def _canonical(value: str) -> str:
    return " ".join(value.split()).casefold()


def _validate_question_model(
    generated: GeneratedQuestion,
    source: SourcePayload,
    *,
    is_active: bool,
    batch: QuizGenerationBatch | None = None,
) -> QuizQuestion:
    question = QuizQuestion(
        batch=batch,
        knowledge_item=source.item,
        domain=generated.domain,
        difficulty=generated.difficulty,
        question_type=generated.question_type,
        prompt=generated.prompt,
        choices=generated.choices,
        correct_choice_ids=generated.correct_choice_ids,
        explanation=generated.explanation,
        evidence_excerpt=generated.evidence_excerpt,
        evidence_digest=generated.evidence_digest,
        source_hash=source.source_hash,
        generator_version=GENERATOR_VERSION,
        model_name=MODEL,
        prompt_version=PROMPT_VERSION,
        prompt_digest=prompt_digest(source.domain),
        publish_state=QuizQuestion.PublishState.PUBLISHED,
        is_active=is_active,
        published_at=timezone.now(),
    )
    question.full_clean(validate_unique=False)
    return question


def prompt_digest(domain: str) -> str:
    return hashlib.sha256(f"{PROMPT_VERSION}\0{domain}".encode()).hexdigest()


def invoke_llm(
    config: llm.LLMConfig,
    source: SourcePayload,
    timeout: int,
) -> GenerationResult:
    prompt = build_prompt(source)
    try:
        response = llm.complete(config, prompt, timeout=timeout)
    except llm.LLMTransportError as error:
        raise QuizGenerationTransientError(error.code) from error
    return GenerationResult(
        questions=parse_generation(response.text, source),
        usage=response.usage,
    )


def generate_with_retries(
    *,
    config: llm.LLMConfig,
    source: SourcePayload,
    timeout: int,
    max_attempts: int = MAX_ATTEMPTS,
    invoker: Callable[[llm.LLMConfig, SourcePayload, int], GenerationResult] = invoke_llm,
) -> tuple[GenerationResult | None, str, int]:
    last_code = "unexpected_error"
    for attempt in range(1, max_attempts + 1):
        try:
            return invoker(config, source, timeout), "", attempt
        except QuizGenerationTransientError as error:
            last_code = error.code
        except QuizGenerationValidationError as error:
            last_code = error.code
    return None, last_code, max_attempts


def run_generation_batch(
    *,
    config: llm.LLMConfig | None,
    timeout: int,
    dry_run: bool = True,
    inventory_only: bool = False,
    domain: str | None = None,
    item_id: int | None = None,
    limit: int = 20,
    aws_allowlisted_external_ids: Iterable[str] = (),
    aws_allowlisted_source_keys: Iterable[str] = (),
    max_attempts: int = MAX_ATTEMPTS,
    invoker: Callable[[llm.LLMConfig, SourcePayload, int], GenerationResult] = invoke_llm,
) -> dict:
    inventory = collect_quiz_inventory(
        aws_allowlisted_external_ids=aws_allowlisted_external_ids,
        aws_allowlisted_source_keys=aws_allowlisted_source_keys,
    )
    candidates = _filter_candidates(inventory.eligible, domain, item_id, limit)
    batch = QuizGenerationBatch.objects.create(
        inventory_version=inventory.inventory_version,
        allowlist_snapshot={
            "aws_external_ids": sorted(set(aws_allowlisted_external_ids)),
            "aws_source_keys": sorted(set(aws_allowlisted_source_keys)),
        },
        dry_run=dry_run,
        status=(
            QuizGenerationBatch.Status.DRY_RUN
            if dry_run
            else QuizGenerationBatch.Status.WRITING
        ),
        candidate_count=len(candidates),
        quarantined_count=len(inventory.quarantined),
        generator_version=GENERATOR_VERSION,
        model_name=MODEL,
        prompt_version=PROMPT_VERSION,
        prompt_digest=prompt_digest(domain or "all"),
        started_at=timezone.now(),
    )
    counts = {
        "quiz_candidates": len(candidates),
        "quiz_published": 0,
        "quiz_quarantined": len(inventory.quarantined),
        "quiz_failed": 0,
        "quiz_dry_run": dry_run,
    }
    outcomes = [
        {
            "source_key": candidate.source_key,
            "status": "quarantined",
            "reason": candidate.reason,
        }
        for candidate in inventory.quarantined
    ]
    if inventory_only:
        _finish_batch(batch, outcomes, counts, status=QuizGenerationBatch.Status.SUCCESS)
        return counts

    for candidate in candidates:
        try:
            source = source_payload(candidate)
            result, error_code, attempts = generate_with_retries(
                config=config,
                source=source,
                timeout=timeout,
                max_attempts=max_attempts,
                invoker=invoker,
            )
            if result is None:
                counts["quiz_failed"] += 1
                outcomes.append(
                    _candidate_outcome(candidate, "failed", error_code, attempts)
                )
                continue
            if dry_run:
                outcomes.append(
                    _candidate_outcome(
                        candidate,
                        "dry_run",
                        "",
                        attempts,
                        generated=len(result.questions),
                    )
                )
                continue
            published, skipped = publish_questions(batch, source, result.questions)
            counts["quiz_published"] += published
            outcomes.append(
                _candidate_outcome(
                    candidate,
                    "published" if published else "duplicate_skipped",
                    "",
                    attempts,
                    generated=len(result.questions),
                    published=published,
                    duplicate_skipped=skipped,
                )
            )
        except QuizGenerationValidationError as error:
            counts["quiz_failed"] += 1
            outcomes.append(_candidate_outcome(candidate, "failed", error.code, 0))
    _finish_batch(batch, outcomes, counts, status=QuizGenerationBatch.Status.SUCCESS)
    return counts


def _filter_candidates(
    candidates: tuple[QuizInventoryCandidate, ...],
    domain: str | None,
    item_id: int | None,
    limit: int,
) -> list[QuizInventoryCandidate]:
    result = [
        candidate
        for candidate in candidates
        if (domain is None or candidate.domain == domain)
        and (item_id is None or candidate.knowledge_item_id == item_id)
    ]
    return result[:limit]


def _candidate_outcome(
    candidate: QuizInventoryCandidate,
    status: str,
    error_code: str,
    attempts: int,
    **extra,
) -> dict:
    outcome = {
        "knowledge_item_id": candidate.knowledge_item_id,
        "source_key": candidate.source_key,
        "source_hash": candidate.source_hash,
        "domain": candidate.domain,
        "status": status,
        "error_code": error_code,
        "attempts": attempts,
        **extra,
    }
    serialized = json.dumps(
        outcome,
        cls=DjangoJSONEncoder,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if len(serialized) > 1024:
        raise QuizGenerationValidationError("candidate_outcome_too_large")
    return outcome


def _finish_batch(
    batch: QuizGenerationBatch,
    outcomes: list[dict],
    counts: dict,
    *,
    status: str,
) -> None:
    batch.status = status
    batch.published_count = counts["quiz_published"]
    batch.quarantined_count = counts["quiz_quarantined"]
    batch.failed_count = counts["quiz_failed"]
    batch.candidate_outcomes = outcomes
    batch.finished_at = timezone.now()
    batch.save(
        update_fields=[
            "status",
            "published_count",
            "quarantined_count",
            "failed_count",
            "candidate_outcomes",
            "finished_at",
            "updated_at",
        ]
    )


def publish_questions(
    batch: QuizGenerationBatch,
    source: SourcePayload,
    questions: tuple[GeneratedQuestion, ...],
) -> tuple[int, int]:
    published = 0
    skipped = 0
    with transaction.atomic():
        try:
            item = KnowledgeItem.objects.select_for_update().get(pk=source.item.pk)
        except KnowledgeItem.DoesNotExist as error:
            raise QuizGenerationValidationError("source_deleted") from error
        if item.source_hash != source.source_hash or not source_text(item):
            raise QuizGenerationValidationError("source_hash_stale")
        QuizQuestion.objects.select_for_update().filter(
            knowledge_item=item,
            domain=source.domain,
            is_active=True,
        ).exclude(source_hash=item.source_hash).update(
            publish_state=QuizQuestion.PublishState.SUPERSEDED,
            is_active=False,
            active_identity_hash=None,
            updated_at=timezone.now(),
        )
        locked_source = SourcePayload(
            item=item,
            domain=source.domain,
            source_key=source.source_key,
            source_hash=item.source_hash,
            title=source.title,
            text=source.text,
        )
        for generated in questions:
            question = _validate_question_model(
                generated,
                locked_source,
                is_active=True,
                batch=batch,
            )
            if QuizQuestion.objects.filter(
                active_identity_hash=question.active_identity_hash
            ).exists():
                skipped += 1
                continue
            try:
                question.save()
            except (IntegrityError, ValidationError):
                skipped += 1
            else:
                published += 1
    return published, skipped
