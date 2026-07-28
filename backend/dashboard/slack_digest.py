import html
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from .models import KnowledgeItem, OperationRun, ScheduleEvent


@dataclass(frozen=True)
class Digest:
    text: str
    actionable_count: int


def build_digest(*, now: datetime | None = None) -> Digest:
    checked_at = now or timezone.now()
    local_day = timezone.localdate(checked_at)
    day_start = timezone.make_aware(
        datetime.combine(local_day, time.min),
        timezone.get_current_timezone(),
    )
    visible = KnowledgeItem.objects.filter(hidden_at__isnull=True).filter(
        Q(content_run__isnull=True) | Q(content_run__hidden_at__isnull=True)
    )
    new_items = visible.filter(generated_at__gte=day_start).count()
    awaiting = visible.filter(
        source_type=KnowledgeItem.SourceType.SLACK_QA,
        status=KnowledgeItem.Status.AWAITING_ANSWER,
    )
    stale = visible.filter(
        Q(verification_status=KnowledgeItem.VerificationStatus.STALE)
        | Q(classification_stale_at__isnull=False)
        | Q(review_due_at__isnull=False, review_due_at__lte=checked_at)
    ).distinct()
    overdue = ScheduleEvent.objects.filter(
        item_type=ScheduleEvent.ItemType.TODO,
        completed=False,
        starts_at__lt=day_start,
    )
    recent_failure = (
        OperationRun.objects.filter(
            status=OperationRun.Status.FAILED,
            started_at__gte=checked_at - timedelta(days=1),
        )
        .order_by("-started_at", "-id")
        .first()
    )
    lines = [
        f"*Knowledge Dashboard · {local_day.isoformat()}*",
        f"• 오늘 추가된 지식: {new_items}",
        f"• 답변 대기: {awaiting.count()}",
        f"• 재검토 필요: {stale.count()}",
        f"• 기한이 지난 할 일: {overdue.count()}",
    ]
    if recent_failure:
        lines.append(
            "• 최근 작업 실패: "
            f"{recent_failure.get_kind_display()} "
            f"({recent_failure.error_code or 'unknown'})"
        )
    waiting_titles = list(awaiting.values_list("title", flat=True)[:3])
    if waiting_titles:
        lines.extend(["", "*먼저 확인할 질문*"])
        lines.extend(f"• {html.escape(title, quote=False)}" for title in waiting_titles)
    actionable_count = awaiting.count() + stale.count() + overdue.count()
    if recent_failure:
        actionable_count += 1
    if actionable_count == 0:
        lines.extend(["", "현재 즉시 처리할 항목이 없습니다."])
    return Digest(text="\n".join(lines), actionable_count=actionable_count)


def send_digest(client, channel: str, digest: Digest) -> dict:
    normalized_channel = str(channel or "").strip()
    if not normalized_channel:
        raise ValueError("Slack 요약 알림 채널이 필요합니다.")
    return client.chat_postMessage(
        channel=normalized_channel,
        text=digest.text,
        unfurl_links=False,
        unfurl_media=False,
    )
