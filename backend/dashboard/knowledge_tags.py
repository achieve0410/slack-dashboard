from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from .models import (
    Category,
    KnowledgeItem,
    KnowledgeTag,
    KnowledgeTagActiveSnapshot,
    KnowledgeTagAssignment,
    KnowledgeTagMutationLock,
    QuizDomainConfig,
)


MIN_TAGS_PER_ITEM = 3


def active_tag_snapshot_id() -> int | None:
    pointer = KnowledgeTagActiveSnapshot.objects.only("snapshot_id").first()
    return pointer.snapshot_id if pointer else None


def tag_labels_by_item(
    item_ids: list[int] | tuple[int, ...],
    *,
    snapshot_id: int | None,
) -> dict[int, list[str]]:
    result = {item_id: [] for item_id in item_ids}
    if not snapshot_id or not item_ids:
        return result
    rows = (
        KnowledgeTagAssignment.objects.filter(
            snapshot_id=snapshot_id,
            knowledge_item_id__in=item_ids,
        )
        .select_related("tag")
        .order_by("knowledge_item_id", "position", "id")
    )
    for assignment in rows:
        result.setdefault(assignment.knowledge_item_id, []).append(assignment.tag.label)
    return result


def item_tag_labels(item: KnowledgeItem) -> list[str]:
    cached = getattr(item, "_active_tag_labels", None)
    if cached is not None:
        return cached
    return []


def attach_tag_labels(
    items: list[KnowledgeItem],
    *,
    snapshot_id: int | None,
) -> list[KnowledgeItem]:
    labels = tag_labels_by_item([item.pk for item in items], snapshot_id=snapshot_id)
    for item in items:
        item._active_tag_labels = labels.get(item.pk, [])
    return items


def normalize_filter_label(label: str) -> str:
    normalized = KnowledgeTag.normalize_label(label)
    if not normalized:
        raise ValidationError("태그를 입력해주세요.")
    return normalized


def normalize_manual_labels(labels) -> list[str]:
    if not isinstance(labels, list):
        raise ValidationError("tags는 배열이어야 합니다.")
    normalized = []
    seen = set()
    for label in labels:
        if not isinstance(label, str):
            raise ValidationError("태그는 문자열이어야 합니다.")
        display_label = KnowledgeTag.display_label(label)
        normalized_label = KnowledgeTag.normalize_label(label)
        if not normalized_label:
            raise ValidationError("태그는 비워둘 수 없습니다.")
        if any(_is_control(character) for character in display_label):
            raise ValidationError("태그에는 제어 문자를 사용할 수 없습니다.")
        if normalized_label in seen:
            raise ValidationError("중복 태그는 사용할 수 없습니다.")
        normalized.append(display_label)
        seen.add(normalized_label)
    if len(normalized) < MIN_TAGS_PER_ITEM:
        raise ValidationError("태그는 최소 3개 이상이어야 합니다.")
    return normalized


def replace_item_tags(item_id: int, labels) -> list[str]:
    normalized_labels = normalize_manual_labels(labels)
    excluded_path_keys = {
        Category.canonical_path_key(path)
        for path in QuizDomainConfig.objects.filter(enabled=True).values_list(
            "category_path",
            flat=True,
        )
    }
    with transaction.atomic():
        KnowledgeTagMutationLock.lock()
        pointer = (
            KnowledgeTagActiveSnapshot.objects.select_for_update()
            .filter(singleton_key=1)
            .first()
        )
        if pointer is None:
            raise ValidationError("활성 태그 스냅샷이 없습니다.")
        item = (
            KnowledgeItem.objects.select_for_update()
            .select_related("category", "content_run")
            .filter(pk=item_id, hidden_at__isnull=True)
            .filter(Q(content_run__isnull=True) | Q(content_run__hidden_at__isnull=True))
            .first()
        )
        if item is None:
            raise ValidationError("태그를 수정할 수 있는 지식 항목이 아닙니다.")
        if item.category_id and item.category.path_key in excluded_path_keys:
            raise ValidationError("학습 지식 태그는 수정할 수 없습니다.")
        snapshot = pointer.snapshot
        KnowledgeTagAssignment.objects.filter(
            snapshot=snapshot,
            knowledge_item=item,
        ).delete()
        for position, label in enumerate(normalized_labels, start=1):
            KnowledgeTagAssignment.objects.create(
                snapshot=snapshot,
                knowledge_item=item,
                tag=KnowledgeTag.for_label(label),
                source=KnowledgeTagAssignment.Source.USER,
                position=position,
            )
        snapshot.item_count = (
            KnowledgeTagAssignment.objects.filter(snapshot=snapshot)
            .values("knowledge_item_id")
            .distinct()
            .count()
        )
        snapshot.tag_count = (
            KnowledgeTagAssignment.objects.filter(snapshot=snapshot)
            .values("tag_id")
            .distinct()
            .count()
        )
        snapshot.assignment_count = KnowledgeTagAssignment.objects.filter(
            snapshot=snapshot,
        ).count()
        snapshot.save(update_fields=["item_count", "tag_count", "assignment_count", "updated_at"])
    return normalized_labels


def _is_control(character: str) -> bool:
    import unicodedata

    return unicodedata.category(character) == "Cc"
