from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Category, KnowledgeItem


def approve_knowledge_items(
    item_ids: list[int],
    category_id: int,
    reviewer,
    review_note: str,
) -> tuple[int, int]:
    note = review_note.strip()
    if not note:
        raise ValidationError("검토 사유를 입력해야 합니다.")

    with transaction.atomic():
        try:
            category = Category.lock_active_chain(category_id)[-1]
        except Category.DoesNotExist:
            raise ValidationError("활성 카테고리를 선택해야 합니다.")

        items = list(
            KnowledgeItem.objects.select_for_update()
            .select_related("content_run")
            .filter(pk__in=item_ids, hidden_at__isnull=True)
            .order_by("pk")
        )
        reviewed_at = timezone.now()
        eligible = []
        for item in items:
            if item.status not in {
                KnowledgeItem.Status.PENDING,
                KnowledgeItem.Status.NEEDS_REVIEW,
                KnowledgeItem.Status.CLASSIFIED,
            }:
                continue
            if (
                item.source_type == KnowledgeItem.SourceType.SLACK_QA
                and not item.answer.strip()
            ):
                continue
            item.category = category
            item.status = KnowledgeItem.Status.CLASSIFIED
            item.classified_at = reviewed_at
            item.reviewed_by = reviewer
            item.reviewed_at = reviewed_at
            item.classification_model = "manual"
            item.classification_confidence = None
            item.classification_reason = f"Manual review: {note}"
            item.classification_stale_at = None
            item.full_clean()
            eligible.append(item)

        for item in eligible:
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
    return len(eligible), len(items) - len(eligible)
