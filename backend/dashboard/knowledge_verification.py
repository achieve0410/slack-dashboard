import os
from collections import Counter
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import KnowledgeFeedback, KnowledgeItem


class VerificationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def effective_verification_status(
    item: KnowledgeItem,
    *,
    now=None,
) -> str:
    if item.verification_status == KnowledgeItem.VerificationStatus.STALE:
        return KnowledgeItem.VerificationStatus.STALE
    checked_at = now or timezone.now()
    if item.classification_stale_at or (
        item.review_due_at and item.review_due_at <= checked_at
    ):
        return KnowledgeItem.VerificationStatus.STALE
    return item.verification_status


def verification_payload(item: KnowledgeItem, *, now=None) -> dict:
    counts = Counter(
        entry.kind for entry in item.feedback_entries.all()
    ) if item.pk else Counter()
    status = effective_verification_status(item, now=now)
    return {
        "status": status,
        "status_label": dict(KnowledgeItem.VerificationStatus.choices)[status],
        "owner": (
            {
                "id": item.verification_owner_id,
                "username": item.verification_owner.get_username(),
            }
            if item.verification_owner_id
            else None
        ),
        "verified_at": item.verified_at,
        "review_due_at": item.review_due_at,
        "note": item.verification_note,
        "feedback_counts": {
            choice: counts.get(choice, 0)
            for choice in KnowledgeFeedback.Kind.values
        },
    }


def update_verification(item_id: int, data: dict, user) -> KnowledgeItem:
    if not isinstance(data, dict):
        raise VerificationError("invalid_request", "요청 본문은 JSON 객체여야 합니다.")
    allowed = {"status", "review_due_at", "note"}
    if not set(data).issubset(allowed) or "status" not in data:
        raise VerificationError(
            "invalid_request",
            "status와 선택적인 review_due_at, note만 지정할 수 있습니다.",
        )
    status = data["status"]
    if status not in KnowledgeItem.VerificationStatus.values:
        raise VerificationError("invalid_status", "지원하지 않는 검증 상태입니다.")
    note = data.get("note", "")
    if not isinstance(note, str) or len(note.strip()) > 1000:
        raise VerificationError("invalid_note", "검증 메모는 1000자 이하여야 합니다.")
    review_due_at = _review_due_at(data.get("review_due_at"), status=status)
    checked_at = timezone.now()
    with transaction.atomic():
        try:
            item = (
                KnowledgeItem.objects.select_for_update()
                .select_related("verification_owner")
                .get(pk=item_id, hidden_at__isnull=True)
            )
        except KnowledgeItem.DoesNotExist as error:
            raise VerificationError("not_found", "지식 항목을 찾을 수 없습니다.") from error
        item.verification_status = status
        item.verification_note = note.strip()
        item.review_due_at = review_due_at
        if status == KnowledgeItem.VerificationStatus.VERIFIED:
            item.verification_owner = user if getattr(user, "is_authenticated", False) else None
            item.verified_at = checked_at
        elif status == KnowledgeItem.VerificationStatus.UNVERIFIED:
            item.verification_owner = None
            item.verified_at = None
        item.full_clean()
        item.save(
            update_fields=[
                "verification_status",
                "verification_note",
                "verification_owner",
                "verified_at",
                "review_due_at",
                "updated_at",
            ]
        )
    return item


def create_feedback(item_id: int, data: dict, user) -> tuple[KnowledgeItem, KnowledgeFeedback]:
    if not isinstance(data, dict) or not set(data).issubset({"kind", "comment"}):
        raise VerificationError(
            "invalid_request",
            "kind와 선택적인 comment만 지정할 수 있습니다.",
        )
    kind = data.get("kind")
    if kind not in KnowledgeFeedback.Kind.values:
        raise VerificationError("invalid_kind", "지원하지 않는 피드백 종류입니다.")
    comment = data.get("comment", "")
    if not isinstance(comment, str) or len(comment.strip()) > 1000:
        raise VerificationError("invalid_comment", "피드백 메모는 1000자 이하여야 합니다.")
    with transaction.atomic():
        try:
            item = (
                KnowledgeItem.objects.select_for_update()
                .select_related("verification_owner")
                .get(pk=item_id, hidden_at__isnull=True)
            )
        except KnowledgeItem.DoesNotExist as error:
            raise VerificationError("not_found", "지식 항목을 찾을 수 없습니다.") from error
        feedback = KnowledgeFeedback(
            knowledge_item=item,
            kind=kind,
            comment=comment.strip(),
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
        feedback.full_clean()
        feedback.save()
        if kind in {
            KnowledgeFeedback.Kind.INCORRECT,
            KnowledgeFeedback.Kind.OUTDATED,
        }:
            item.verification_status = KnowledgeItem.VerificationStatus.STALE
            item.save(update_fields=["verification_status", "updated_at"])
    return item, feedback


def _review_due_at(value, *, status: str):
    if value in (None, ""):
        if status != KnowledgeItem.VerificationStatus.VERIFIED:
            return None
        try:
            days = int(os.getenv("KNOWLEDGE_REVIEW_INTERVAL_DAYS", "90"))
        except ValueError as error:
            raise VerificationError(
                "invalid_configuration",
                "KNOWLEDGE_REVIEW_INTERVAL_DAYS는 정수여야 합니다.",
            ) from error
        if not 1 <= days <= 3650:
            raise VerificationError(
                "invalid_configuration",
                "KNOWLEDGE_REVIEW_INTERVAL_DAYS는 1~3650이어야 합니다.",
            )
        return timezone.now() + timedelta(days=days)
    if not isinstance(value, str):
        raise VerificationError("invalid_review_due_at", "검토 예정일 형식이 잘못되었습니다.")
    parsed = parse_datetime(value)
    if parsed is None:
        raise VerificationError("invalid_review_due_at", "검토 예정일 형식이 잘못되었습니다.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed
