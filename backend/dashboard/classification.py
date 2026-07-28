import json
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import llm
from .models import Category, KnowledgeItem


MODEL = llm.configured_model_name()
PROVIDER = llm.configured_provider_name()
MAX_BATCH_SIZE = 100
EXPECTED_RESPONSE_KEYS = {
    "title",
    "summary",
    "category_id",
    "new_category_path",
    "confidence",
    "reason",
}


class ClassifierValidationError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class TransientInferenceError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ReviewRequired(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ClassificationDecision:
    title: str
    summary: str
    category_id: int | None
    new_category_path: tuple[str, ...]
    confidence: Decimal
    reason: str


@dataclass(frozen=True)
class InferenceResult:
    decision: ClassificationDecision
    usage: dict


@contextmanager
def classifier_lock(path: Path) -> Iterator[bool]:
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


def active_category_catalog() -> list[dict]:
    return list(
        Category.objects.filter(pk__in=Category.active_tree_ids())
        .order_by("path")
        .values("id", "path", "depth")
    )


def build_prompt(item: KnowledgeItem, catalog: list[dict]) -> str:
    if item.source_type == KnowledgeItem.SourceType.CRON:
        item_text = {
            "source_type": item.source_type,
            "title": item.content_run.title,
            "body": item.content_run.body,
        }
    else:
        item_text = {
            "source_type": item.source_type,
            "question": item.question,
            "answer": item.answer,
        }
    payload = {
        "instruction": (
            "Classify the item using the existing categories when possible. "
            "Return exactly one JSON object with no markdown or extra prose."
        ),
        "item": item_text,
        "active_categories": catalog,
        "response_contract": {
            "title": "non-empty string, maximum 250 characters",
            "summary": "non-empty string, maximum 600 characters",
            "category_id": "positive existing ID or null",
            "new_category_path": "empty list or 1-3 category segments",
            "confidence": "number from 0 to 1",
            "reason": "non-empty string, maximum 1000 characters",
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ClassifierValidationError("duplicate_key")
        result[key] = value
    return result


def _invalid_constant(value: str):
    raise ClassifierValidationError("invalid_number")


def _normalize_path(raw_path) -> tuple[str, ...]:
    if not isinstance(raw_path, list) or len(raw_path) > 3:
        raise ClassifierValidationError("invalid_path_depth")
    normalized = []
    path_max_length = Category._meta.get_field("path").max_length
    path_key_max_length = Category._meta.get_field("path_key").max_length
    for segment in raw_path:
        if not isinstance(segment, str):
            raise ClassifierValidationError("invalid_path_segment")
        value = Category.normalize_segment(segment)
        if (
            not value
            or "/" in value
            or any(unicodedata.category(character) == "Cc" for character in value)
        ):
            raise ClassifierValidationError("invalid_path_segment")
        if len(value) > Category._meta.get_field("name").max_length:
            raise ClassifierValidationError("invalid_path_segment_length")
        normalized.append(value)
        display_path = "/".join(normalized)
        if len(display_path) > path_max_length:
            raise ClassifierValidationError("invalid_path_length")
        if len(Category.canonical_path_key(display_path)) > path_key_max_length:
            raise ClassifierValidationError("invalid_path_key_length")
    return tuple(normalized)


def parse_decision(raw_output: str) -> ClassificationDecision:
    try:
        payload = json.loads(
            raw_output,
            parse_float=Decimal,
            parse_constant=_invalid_constant,
            object_pairs_hook=_strict_object,
        )
    except ClassifierValidationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ClassifierValidationError("invalid_json") from error
    if not isinstance(payload, dict) or set(payload) != EXPECTED_RESPONSE_KEYS:
        raise ClassifierValidationError("invalid_schema")

    title = payload["title"]
    summary = payload["summary"]
    reason = payload["reason"]
    if not isinstance(title, str) or not title.strip() or len(title) > 250:
        raise ClassifierValidationError("invalid_title")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 600:
        raise ClassifierValidationError("invalid_summary")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ClassifierValidationError("invalid_reason")

    category_id = payload["category_id"]
    if isinstance(category_id, bool) or (
        category_id is not None
        and (not isinstance(category_id, int) or category_id <= 0)
    ):
        raise ClassifierValidationError("invalid_category_id")
    path = _normalize_path(payload["new_category_path"])
    if (category_id is None) == (not path):
        raise ClassifierValidationError("ambiguous_category")

    confidence = payload["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, Decimal)):
        raise ClassifierValidationError("invalid_confidence")
    try:
        confidence = Decimal(confidence)
    except (InvalidOperation, ValueError) as error:
        raise ClassifierValidationError("invalid_confidence") from error
    if not confidence.is_finite() or confidence < 0 or confidence > 1:
        raise ClassifierValidationError("invalid_confidence")

    return ClassificationDecision(
        title=title.strip(),
        summary=summary.strip(),
        category_id=category_id,
        new_category_path=path,
        confidence=confidence,
        reason=reason.strip(),
    )


def invoke_llm(
    config: llm.LLMConfig,
    item: KnowledgeItem,
    catalog: list[dict],
    timeout: int,
) -> InferenceResult:
    prompt = build_prompt(item, catalog)
    try:
        response = llm.complete(
            config,
            prompt,
            timeout=timeout,
            operation="classify",
        )
    except llm.LLMTransportError as error:
        raise TransientInferenceError(error.code) from error
    return InferenceResult(
        decision=parse_decision(response.text),
        usage=response.usage,
    )


def eligible_pending_items(item_id: int | None, limit: int) -> list[KnowledgeItem]:
    queryset = (
        KnowledgeItem.objects.filter(
            status=KnowledgeItem.Status.PENDING,
            hidden_at__isnull=True,
        )
        .select_related("content_run")
        .order_by("generated_at", "id")
    )
    if item_id is not None:
        queryset = queryset.filter(pk=item_id)
    eligible = []
    for item in queryset.iterator():
        if item.source_type == KnowledgeItem.SourceType.SLACK_QA:
            allowed = bool(item.answer.strip())
        else:
            allowed = bool(
                item.content_run
                and item.content_run.status == "success"
                and item.content_run.body.strip()
            )
        if allowed:
            eligible.append(item)
        if len(eligible) == limit:
            break
    return eligible


def _mark_needs_review(
    item: KnowledgeItem,
    code: str,
    decision: ClassificationDecision | None = None,
) -> None:
    item.status = KnowledgeItem.Status.NEEDS_REVIEW
    item.category = None
    item.classification_model = MODEL
    item.classification_confidence = (
        decision.confidence.quantize(Decimal("0.001")) if decision else None
    )
    reason = f"Classifier review required: {code}"
    if decision:
        reason = f"{reason}. {decision.reason}"
    item.classification_reason = reason[:1000]
    item.classified_at = None
    item.reviewed_by = None
    item.reviewed_at = None
    item.classification_stale_at = None
    update_fields = [
        "status",
        "category",
        "classification_model",
        "classification_confidence",
        "classification_reason",
        "classified_at",
        "reviewed_by",
        "reviewed_at",
        "classification_stale_at",
        "updated_at",
    ]
    if decision:
        item.title = decision.title
        item.summary = (
            ""
            if item.source_type == KnowledgeItem.SourceType.SLACK_QA
            else decision.summary
        )
        update_fields.extend(("title", "summary"))
    item.save(update_fields=update_fields)


def mark_invalid_output(item_id: int, expected_hash: str, code: str) -> str:
    with transaction.atomic():
        item = KnowledgeItem.objects.select_for_update().get(pk=item_id)
        if item.status != KnowledgeItem.Status.PENDING or item.source_hash != expected_hash:
            return "stale"
        _mark_needs_review(item, code)
    return "needs_review"


def _prefixes(path: tuple[str, ...]) -> list[tuple[str, str, str, int]]:
    result = []
    segments = []
    for depth, name in enumerate(path, start=1):
        segments.append(name)
        display_path = "/".join(segments)
        path_key = Category.canonical_path_key(display_path)
        result.append(
            (display_path, path_key, Category.identity_digest(path_key), depth)
        )
    return result


def _create_category_path(
    decision: ClassificationDecision,
    observation: dict,
) -> Category:
    prefixes = _prefixes(decision.new_category_path)
    path_keys = [path_key for _, path_key, _, _ in prefixes]
    try:
        existing = Category.exact_categories(path_keys)
    except Category.DoesNotExist as error:
        raise ReviewRequired("category_identity_collision") from error

    deepest_existing = next(
        (existing[path_key] for path_key in reversed(path_keys) if path_key in existing),
        None,
    )
    locked_chain = ()
    if deepest_existing:
        try:
            locked_chain = Category.lock_active_chain(deepest_existing.pk)
            existing = Category.exact_categories(path_keys, for_update=True)
        except Category.DoesNotExist as error:
            raise ReviewRequired("inactive_category_path") from error
        expected_existing = [
            existing.get(path_key)
            for path_key in path_keys[: len(locked_chain)]
        ]
        if any(category is None for category in expected_existing) or tuple(
            category.pk for category in expected_existing
        ) != tuple(category.pk for category in locked_chain):
            raise ReviewRequired("invalid_existing_prefix")

    full_existing = existing.get(path_keys[-1])
    if full_existing:
        if full_existing.depth != len(prefixes):
            raise ReviewRequired("inactive_category_path")
        if decision.confidence < Decimal("0.65"):
            raise ReviewRequired("low_confidence_existing_path")
        observation["category_reused"] += 1
        return full_existing

    first_missing_depth = next(
        depth for _, path_key, _, depth in prefixes if path_key not in existing
    )
    for _, path_key, _, depth in prefixes[: first_missing_depth - 1]:
        category = existing[path_key]
        if category.depth != depth:
            raise ReviewRequired("invalid_existing_prefix")
    threshold = Decimal("0.85") if first_missing_depth == 1 else Decimal("0.75")
    if decision.confidence < threshold:
        raise ReviewRequired("low_confidence_new_path")

    parent = None
    for _, path_key, _, depth in prefixes:
        category = existing.get(path_key)
        if category:
            if (
                category.depth != depth
                or category.parent_id != (parent.pk if parent else None)
            ):
                raise ReviewRequired("invalid_existing_prefix")
            observation["category_reused"] += 1
            parent = category
            continue
        name = decision.new_category_path[depth - 1]
        display_path = f"{parent.path}/{name}" if parent else name
        try:
            with transaction.atomic():
                category = Category.objects.create(
                    name=name,
                    path=display_path,
                    path_key=path_key,
                    parent=parent,
                    depth=depth,
                    created_by=Category.CreatedBy.AI,
                    is_active=True,
                )
        except IntegrityError:
            try:
                category = Category.exact_category(path_key)
                chain = Category.lock_active_chain(category.pk)
                category = chain[-1]
            except Category.DoesNotExist as error:
                raise ReviewRequired("category_path_race_conflict") from error
            if (
                category.depth != depth
                or category.parent_id != (parent.pk if parent else None)
            ):
                raise ReviewRequired("category_path_race_conflict")
            observation["category_reused"] += 1
        else:
            observation["category_created"] += 1
            observation["category_growth_paths"].append(display_path)
        existing[path_key] = category
        parent = category
    return parent


def _resolve_category(decision: ClassificationDecision, observation: dict) -> Category:
    if decision.category_id is not None:
        try:
            category = Category.lock_active_chain(decision.category_id)[-1]
        except Category.DoesNotExist:
            raise ReviewRequired("unknown_or_inactive_category")
        if decision.confidence < Decimal("0.65"):
            raise ReviewRequired("low_confidence_existing_category")
        observation["category_existing"] += 1
        return category
    return _create_category_path(decision, observation)


def apply_decision(
    item_id: int,
    expected_hash: str,
    decision: ClassificationDecision,
    observation: dict | None = None,
) -> str:
    if observation is None:
        observation = {}
    observation.update(
        category_created=0,
        category_reused=0,
        category_existing=0,
        category_growth_paths=[],
    )
    with transaction.atomic():
        item = KnowledgeItem.objects.select_for_update().get(pk=item_id)
        if item.status != KnowledgeItem.Status.PENDING or item.source_hash != expected_hash:
            return "stale"
        try:
            with transaction.atomic():
                category = _resolve_category(decision, observation)
        except ReviewRequired as error:
            observation.update(
                category_created=0,
                category_reused=0,
                category_existing=0,
                category_growth_paths=[],
            )
            _mark_needs_review(item, error.code, decision)
            return "needs_review"

        item.title = decision.title
        item.summary = (
            ""
            if item.source_type == KnowledgeItem.SourceType.SLACK_QA
            else decision.summary
        )
        item.category = category
        item.status = KnowledgeItem.Status.CLASSIFIED
        item.classification_model = MODEL
        item.classification_confidence = decision.confidence.quantize(Decimal("0.001"))
        item.classification_reason = decision.reason
        item.classified_at = timezone.now()
        item.reviewed_by = None
        item.reviewed_at = None
        item.classification_stale_at = None
        item.save(
            update_fields=[
                "title",
                "summary",
                "category",
                "status",
                "classification_model",
                "classification_confidence",
                "classification_reason",
                "classified_at",
                "reviewed_by",
                "reviewed_at",
                "classification_stale_at",
                "updated_at",
            ]
        )
    return "classified"
