from collections.abc import Iterable
from datetime import datetime, time, timedelta
from enum import StrEnum

from django.utils import timezone


class AgendaGroup(StrEnum):
    COMPLETED = "completed"
    TODAY = "today"
    OVERDUE_TODO = "overdue_todo"
    PAST_SCHEDULE = "past_schedule"
    UPCOMING = "upcoming"
    UNDATED = "undated"


GROUP_ORDER = tuple(AgendaGroup)
GROUP_LABELS = {
    AgendaGroup.COMPLETED: "완료",
    AgendaGroup.TODAY: "오늘",
    AgendaGroup.OVERDUE_TODO: "지연 TODO",
    AgendaGroup.PAST_SCHEDULE: "지난 일정",
    AgendaGroup.UPCOMING: "예정",
    AgendaGroup.UNDATED: "기한 없음",
}


def _aware(value: datetime, current_timezone) -> datetime:
    if timezone.is_naive(value):
        return timezone.make_aware(value, current_timezone)
    return value


def agenda_group(event, *, now: datetime | None = None) -> AgendaGroup:
    if event.completed:
        return AgendaGroup.COMPLETED

    current_timezone = timezone.get_current_timezone()
    local_now = timezone.localtime(_aware(now or timezone.now(), current_timezone))
    today_start = timezone.make_aware(
        datetime.combine(local_now.date(), time.min),
        current_timezone,
    )
    tomorrow_start = timezone.make_aware(
        datetime.combine(local_now.date() + timedelta(days=1), time.min),
        current_timezone,
    )

    if event.item_type == "todo":
        if event.starts_at is None:
            return AgendaGroup.UNDATED
        due_at = _aware(event.starts_at, current_timezone)
        if due_at < today_start:
            return AgendaGroup.OVERDUE_TODO
        if due_at < tomorrow_start:
            return AgendaGroup.TODAY
        return AgendaGroup.UPCOMING

    if event.item_type != "schedule" or event.starts_at is None:
        raise ValueError("일정 그룹은 유효한 schedule 또는 todo에만 계산할 수 있습니다.")

    starts_at = _aware(event.starts_at, current_timezone)
    ends_at = _aware(event.ends_at, current_timezone) if event.ends_at else starts_at
    if starts_at < tomorrow_start and ends_at >= today_start:
        return AgendaGroup.TODAY
    if ends_at < today_start:
        return AgendaGroup.PAST_SCHEDULE
    return AgendaGroup.UPCOMING


def agenda_group_counts(events: Iterable, *, now: datetime | None = None) -> dict[str, int]:
    counts = {group.value: 0 for group in GROUP_ORDER}
    for event in events:
        counts[agenda_group(event, now=now).value] += 1
    return counts
