import hashlib
import json
import secrets
from datetime import datetime, timedelta

from django.apps import apps
from django.db import transaction
from django.db.models import Q
from django.utils import timezone


MAX_TARGETS = 1000
SNAPSHOT_TTL = timedelta(minutes=10)
UNDO_WINDOW = timedelta(seconds=10)
CONSUMED_RETENTION = timedelta(days=7)
EXPIRED_RETENTION = timedelta(hours=24)
STATE_ACTION_FIELDS = {
    "read": "read_at",
    "bookmarked": "bookmarked_at",
    "completed": "completed_at",
    "archived": "archived_at",
}
ACTION_TYPES = frozenset({*STATE_ACTION_FIELDS, "category", "hide"})


class KnowledgeActionError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def normalize_target_ids(raw_ids, *, allow_empty: bool = False) -> list[int]:
    if not isinstance(raw_ids, (list, tuple)):
        raise KnowledgeActionError(400, "invalid_ids", "대상 IDs는 배열이어야 합니다.")
    normalized = set()
    for raw_id in raw_ids:
        if isinstance(raw_id, bool):
            raise KnowledgeActionError(400, "invalid_ids", "대상 ID는 양의 정수여야 합니다.")
        if isinstance(raw_id, int):
            item_id = raw_id
        elif isinstance(raw_id, str) and raw_id.strip().isdigit():
            item_id = int(raw_id.strip())
        else:
            raise KnowledgeActionError(400, "invalid_ids", "대상 ID는 양의 정수여야 합니다.")
        if item_id <= 0:
            raise KnowledgeActionError(400, "invalid_ids", "대상 ID는 양의 정수여야 합니다.")
        normalized.add(item_id)
    if not allow_empty and not normalized:
        raise KnowledgeActionError(400, "invalid_ids", "대상을 하나 이상 선택해야 합니다.")
    if len(normalized) > MAX_TARGETS:
        raise KnowledgeActionError(400, "too_many_targets", "대상은 최대 1,000개입니다.")
    return sorted(normalized)


def canonical_action_parameters(action_type: str, parameters) -> dict:
    if action_type not in ACTION_TYPES:
        raise KnowledgeActionError(400, "invalid_action", "지원하지 않는 bulk action입니다.")
    if not isinstance(parameters, dict):
        raise KnowledgeActionError(400, "invalid_parameters", "action parameters는 객체여야 합니다.")
    if action_type in STATE_ACTION_FIELDS:
        if set(parameters) != {"value"} or not isinstance(parameters["value"], bool):
            raise KnowledgeActionError(
                400,
                "invalid_parameters",
                "상태 action은 boolean value만 허용합니다.",
            )
        return {"value": parameters["value"]}
    if action_type == "category":
        category_id = parameters.get("category_id")
        if (
            set(parameters) != {"category_id"}
            or isinstance(category_id, bool)
            or not isinstance(category_id, int)
            or category_id <= 0
        ):
            raise KnowledgeActionError(
                400,
                "invalid_parameters",
                "category action은 양의 정수 category_id만 허용합니다.",
            )
        return {"category_id": category_id}
    if parameters:
        raise KnowledgeActionError(400, "invalid_parameters", "hide action parameters는 비어야 합니다.")
    return {}


def validate_review_note(review_note) -> str:
    if not isinstance(review_note, str):
        raise KnowledgeActionError(400, "invalid_review_note", "검토 사유는 문자열이어야 합니다.")
    note = review_note.strip()
    if not note:
        raise KnowledgeActionError(400, "invalid_review_note", "검토 사유를 입력해야 합니다.")
    if len(note) > 1000:
        raise KnowledgeActionError(400, "invalid_review_note", "검토 사유는 1,000자 이하여야 합니다.")
    return note


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def target_digest(target_ids: list[int]) -> str:
    encoded = json.dumps(target_ids, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_model():
    return apps.get_model("dashboard", "BulkSelectionSnapshot")


def create_snapshot(
    target_ids,
    action_type: str,
    parameters,
    canonical_filter: dict,
    *,
    model=None,
    now: datetime | None = None,
    token_factory=None,
):
    normalized_ids = normalize_target_ids(target_ids)
    normalized_parameters = canonical_action_parameters(action_type, parameters)
    if not isinstance(canonical_filter, dict):
        raise KnowledgeActionError(400, "invalid_filter", "canonical filter는 객체여야 합니다.")
    raw_token = (token_factory or secrets.token_urlsafe)(32)
    created_at = now or timezone.now()
    snapshot_model = model or _snapshot_model()
    snapshot = snapshot_model.objects.create(
        token_hash=token_hash(raw_token),
        target_ids=normalized_ids,
        target_digest=target_digest(normalized_ids),
        target_count=len(normalized_ids),
        action_type=action_type,
        action_parameters=normalized_parameters,
        canonical_filter=canonical_filter,
        expires_at=created_at + SNAPSHOT_TTL,
        affected_ids=[],
    )
    return raw_token, snapshot


def validate_snapshot_execution(
    snapshot,
    current_target_ids,
    action_type: str,
    parameters,
    *,
    now: datetime | None = None,
) -> list[int]:
    normalized_parameters = canonical_action_parameters(action_type, parameters)
    checked_at = now or timezone.now()
    if snapshot.consumed_at is not None:
        raise KnowledgeActionError(409, "snapshot_consumed", "이미 사용한 bulk snapshot입니다.")
    if checked_at >= snapshot.expires_at:
        raise KnowledgeActionError(409, "snapshot_expired", "bulk snapshot이 만료되었습니다.")
    if action_type != snapshot.action_type or normalized_parameters != snapshot.action_parameters:
        raise KnowledgeActionError(
            409,
            "snapshot_action_changed",
            "preview와 다른 bulk action은 실행할 수 없습니다.",
        )

    stored_ids = normalize_target_ids(snapshot.target_ids)
    if snapshot.target_count != len(stored_ids) or snapshot.target_digest != target_digest(stored_ids):
        raise KnowledgeActionError(409, "snapshot_invalid", "bulk snapshot 대상이 손상되었습니다.")
    current_ids = normalize_target_ids(current_target_ids, allow_empty=True)
    if current_ids != stored_ids:
        raise KnowledgeActionError(
            409,
            "snapshot_membership_changed",
            "preview 이후 대상 membership이 변경되었습니다.",
        )
    return stored_ids


def _lock_visible_items(item_ids: list[int]):
    knowledge_model = apps.get_model("dashboard", "KnowledgeItem")
    items = list(
        knowledge_model.objects.select_for_update()
        .select_related("content_run")
        .filter(pk__in=item_ids)
        .order_by("pk")
    )
    if [item.pk for item in items] != item_ids or any(
        item.hidden_at is not None
        or (item.content_run_id and item.content_run.hidden_at is not None)
        for item in items
    ):
        raise KnowledgeActionError(
            409,
            "target_changed",
            "preview 이후 대상 visibility가 변경되었습니다.",
        )
    return items


def _apply_state(items, action_type: str, value: bool, changed_at: datetime) -> None:
    state_model = apps.get_model("dashboard", "KnowledgeConsumptionState")
    item_ids = [item.pk for item in items]
    states = {
        state.knowledge_item_id: state
        for state in state_model.objects.select_for_update().filter(knowledge_item_id__in=item_ids)
    }
    field = STATE_ACTION_FIELDS[action_type]
    for item_id in item_ids:
        state = states.get(item_id)
        if state is None:
            if not value:
                continue
            state = state_model(knowledge_item_id=item_id)
        current = getattr(state, field)
        if value and current is not None or not value and current is None:
            continue
        setattr(state, field, changed_at if value else None)
        if state.pk:
            state.save(update_fields=(field, "updated_at"))
        else:
            state.save(force_insert=True)


def _apply_category(items, category_id: int, reviewer, note: str, changed_at: datetime) -> None:
    category_model = apps.get_model("dashboard", "Category")
    knowledge_model = apps.get_model("dashboard", "KnowledgeItem")
    try:
        category = category_model.lock_active_chain(category_id)[-1]
    except category_model.DoesNotExist as error:
        raise KnowledgeActionError(
            409,
            "category_changed",
            "preview 이후 카테고리가 비활성화되었거나 삭제되었습니다.",
        ) from error

    eligible_statuses = {
        knowledge_model.Status.PENDING,
        knowledge_model.Status.NEEDS_REVIEW,
        knowledge_model.Status.CLASSIFIED,
    }
    if any(
        item.status not in eligible_statuses
        or (
            item.source_type == knowledge_model.SourceType.SLACK_QA
            and not item.answer.strip()
        )
        for item in items
    ):
        raise KnowledgeActionError(
            409,
            "classification_target_changed",
            "분류 대상의 answer 또는 eligibility가 변경되었습니다.",
        )

    for item in items:
        item.category = category
        item.status = knowledge_model.Status.CLASSIFIED
        item.classified_at = changed_at
        item.reviewed_by = reviewer
        item.reviewed_at = changed_at
        item.classification_model = "manual"
        item.classification_confidence = None
        item.classification_reason = note
        item.classification_stale_at = None
        item.full_clean()
    for item in items:
        item.save(
            update_fields=(
                "category",
                "status",
                "classified_at",
                "reviewed_by",
                "reviewed_at",
                "classification_model",
                "classification_confidence",
                "classification_reason",
                "classification_stale_at",
                "updated_at",
            )
        )


def _apply_hide(items, changed_at: datetime) -> None:
    for item in items:
        item.hidden_at = changed_at
        item.save(update_fields=("hidden_at", "updated_at"))
        if item.content_run_id:
            item.content_run.hidden_at = changed_at
            item.content_run.save(update_fields=("hidden_at", "updated_at"))


def apply_knowledge_action(
    item_ids,
    action_type: str,
    parameters,
    *,
    reviewer=None,
    review_note: str = "",
    now: datetime | None = None,
) -> list[int]:
    normalized_ids = normalize_target_ids(item_ids)
    normalized_parameters = canonical_action_parameters(action_type, parameters)
    note = validate_review_note(review_note) if action_type == "category" else ""
    changed_at = now or timezone.now()
    with transaction.atomic():
        items = _lock_visible_items(normalized_ids)
        if action_type in STATE_ACTION_FIELDS:
            _apply_state(items, action_type, normalized_parameters["value"], changed_at)
        elif action_type == "category":
            _apply_category(
                items,
                normalized_parameters["category_id"],
                reviewer,
                note,
                changed_at,
            )
        else:
            _apply_hide(items, changed_at)
    return normalized_ids


def execute_snapshot(
    raw_token: str,
    action_type: str,
    parameters,
    membership_resolver,
    *,
    reviewer=None,
    review_note: str = "",
    model=None,
    now: datetime | None = None,
):
    checked_at = now or timezone.now()
    snapshot_model = model or _snapshot_model()
    with transaction.atomic():
        try:
            snapshot = snapshot_model.objects.select_for_update().get(
                token_hash=token_hash(raw_token)
            )
        except snapshot_model.DoesNotExist as error:
            raise KnowledgeActionError(409, "snapshot_invalid", "bulk snapshot을 찾을 수 없습니다.") from error
        if snapshot.canonical_filter:
            stored_ids = normalize_target_ids(snapshot.target_ids)
            _lock_visible_items(stored_ids)
            state_model = apps.get_model("dashboard", "KnowledgeConsumptionState")
            list(
                state_model.objects.select_for_update()
                .filter(knowledge_item_id__in=stored_ids)
                .order_by("knowledge_item_id")
            )
        current_ids = membership_resolver(snapshot)
        stored_ids = validate_snapshot_execution(
            snapshot,
            current_ids,
            action_type,
            parameters,
            now=checked_at,
        )
        affected_ids = apply_knowledge_action(
            stored_ids,
            action_type,
            parameters,
            reviewer=reviewer,
            review_note=review_note,
            now=checked_at,
        )
        snapshot.consumed_at = checked_at
        snapshot.affected_ids = affected_ids
        snapshot.save(update_fields=("consumed_at", "affected_ids", "updated_at"))
    return snapshot


def restore_items(item_ids) -> list[int]:
    normalized_ids = normalize_target_ids(item_ids)
    knowledge_model = apps.get_model("dashboard", "KnowledgeItem")
    with transaction.atomic():
        items = list(
            knowledge_model.objects.select_for_update()
            .select_related("content_run")
            .filter(pk__in=normalized_ids)
            .order_by("pk")
        )
        if [item.pk for item in items] != normalized_ids:
            raise KnowledgeActionError(409, "restore_target_missing", "복원 대상을 찾을 수 없습니다.")
        for item in items:
            if item.hidden_at is not None:
                item.hidden_at = None
                item.save(update_fields=("hidden_at", "updated_at"))
            if item.content_run_id and item.content_run.hidden_at is not None:
                item.content_run.hidden_at = None
                item.content_run.save(update_fields=("hidden_at", "updated_at"))
    return normalized_ids


def undo_hide_snapshot(
    raw_token: str,
    *,
    model=None,
    now: datetime | None = None,
) -> list[int]:
    checked_at = now or timezone.now()
    snapshot_model = model or _snapshot_model()
    with transaction.atomic():
        try:
            snapshot = snapshot_model.objects.select_for_update().get(
                token_hash=token_hash(raw_token)
            )
        except snapshot_model.DoesNotExist as error:
            raise KnowledgeActionError(409, "snapshot_invalid", "bulk snapshot을 찾을 수 없습니다.") from error
        if snapshot.action_type != "hide" or snapshot.consumed_at is None:
            raise KnowledgeActionError(409, "undo_unavailable", "복원할 hide 실행이 없습니다.")
        if checked_at > snapshot.consumed_at + UNDO_WINDOW:
            raise KnowledgeActionError(409, "undo_expired", "hide 실행의 10초 복원 시간이 지났습니다.")
        return restore_items(snapshot.affected_ids)


def prune_bulk_snapshots(*, model=None, now: datetime | None = None) -> int:
    checked_at = now or timezone.now()
    snapshot_model = model or _snapshot_model()
    deleted, _ = snapshot_model.objects.filter(
        Q(consumed_at__lt=checked_at - CONSUMED_RETENTION)
        | Q(
            consumed_at__isnull=True,
            expires_at__lt=checked_at - EXPIRED_RETENTION,
        )
    ).delete()
    return deleted
