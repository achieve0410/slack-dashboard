import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from .models import ScheduleCategory, ScheduleEvent


MENTION_PREFIX_RE = re.compile(r"^(?:<@[A-Z0-9]+>\s*)+")
TIME_RANGE_RE = re.compile(
    r"^(?P<start>\d{2}:\d{2})(?:\s*[~\-]\s*(?P<end>\d{2}:\d{2}))?$"
)
TITLE_TOKEN_RE = re.compile(r"[0-9a-z가-힣]+")


@dataclass(frozen=True)
class ParsedSchedule:
    title: str
    item_type: str
    todo_category_id: int | None
    starts_at: datetime | None
    ends_at: datetime | None
    all_day: bool
    notes: str


def _keyword_matches(normalized: str, tokens: set[str], keyword: str) -> bool:
    return keyword in tokens if len(keyword) == 1 else keyword in normalized


def infer_todo_category(
    title: str,
    categories: list[ScheduleCategory] | None = None,
) -> ScheduleCategory:
    normalized = title.casefold()
    tokens = set(TITLE_TOKEN_RE.findall(normalized))
    available = categories or list(ScheduleCategory.objects.all())
    fallback = next((category for category in available if category.is_fallback), None)
    for category in available:
        if category.is_fallback:
            continue
        if any(_keyword_matches(normalized, tokens, keyword) for keyword in category.keywords):
            return category
    if fallback:
        return fallback
    raise ScheduleCategory.DoesNotExist("기본 TODO 카테고리가 없습니다.")


def reclassify_automatic_todos() -> int:
    categories = list(ScheduleCategory.objects.all())
    updated = 0
    for event in ScheduleEvent.objects.filter(
        item_type=ScheduleEvent.ItemType.TODO,
        todo_category_manual=False,
    ).only("id", "title", "todo_category_id"):
        category = infer_todo_category(event.title, categories)
        if event.todo_category_id != category.pk:
            ScheduleEvent.objects.filter(pk=event.pk).update(todo_category=category)
            updated += 1
    return updated


def parse_schedule_message(text: str) -> ParsedSchedule | None:
    content = MENTION_PREFIX_RE.sub("", text.strip()).strip()
    parts = [part.strip() for part in content.split("|", 2)]
    if len(parts) < 2:
        return None

    timing, title = parts[:2]
    notes = parts[2] if len(parts) == 3 else ""
    title = title[:200]
    notes = notes[:5000]
    if not title:
        return None

    if timing.casefold() == "todo":
        return ParsedSchedule(
            title=title,
            item_type=ScheduleEvent.ItemType.TODO,
            todo_category_id=infer_todo_category(title).pk,
            starts_at=None,
            ends_at=None,
            all_day=False,
            notes=notes,
        )

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", timing):
        try:
            due_date = datetime.strptime(timing, "%Y-%m-%d")
        except ValueError:
            return None
        return ParsedSchedule(
            title=title,
            item_type=ScheduleEvent.ItemType.TODO,
            todo_category_id=infer_todo_category(title).pk,
            starts_at=timezone.make_aware(
                due_date,
                timezone.get_current_timezone(),
            ),
            ends_at=None,
            all_day=True,
            notes=notes,
        )

    timing_parts = timing.split()
    if len(timing_parts) != 2 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", timing_parts[0]):
        return None

    date_value, time_value = timing_parts
    try:
        if time_value == "종일":
            start = datetime.strptime(date_value, "%Y-%m-%d")
            return ParsedSchedule(
                title=title,
                item_type=ScheduleEvent.ItemType.SCHEDULE,
                todo_category_id=None,
                starts_at=timezone.make_aware(start, timezone.get_current_timezone()),
                ends_at=None,
                all_day=True,
                notes=notes,
            )

        match = TIME_RANGE_RE.fullmatch(time_value)
        if not match:
            return None
        start = datetime.strptime(
            f"{date_value} {match.group('start')}",
            "%Y-%m-%d %H:%M",
        )
        end = (
            datetime.strptime(
                f"{date_value} {match.group('end')}",
                "%Y-%m-%d %H:%M",
            )
            if match.group("end")
            else None
        )
    except ValueError:
        return None

    current_timezone = timezone.get_current_timezone()
    starts_at = timezone.make_aware(start, current_timezone)
    ends_at = timezone.make_aware(end, current_timezone) if end else None
    if ends_at and ends_at < starts_at:
        return None
    return ParsedSchedule(
        title=title,
        item_type=ScheduleEvent.ItemType.SCHEDULE,
        todo_category_id=None,
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=False,
        notes=notes,
    )


def reconcile_schedule_channel(
    channel_id: str,
    messages: list[dict],
    bot_user_id: str,
) -> dict[str, int]:
    stats = {"created": 0, "updated": 0, "unchanged": 0, "deleted": 0, "skipped": 0}
    active_timestamps: set[str] = set()

    with transaction.atomic():
        for message in messages:
            message_ts = str(message.get("ts", ""))
            thread_ts = str(message.get("thread_ts", ""))
            is_reply = bool(thread_ts and thread_ts != message_ts)
            is_bot = (
                message.get("user") == bot_user_id
                or bool(message.get("bot_id"))
                or message.get("subtype") == "bot_message"
            )
            if not message_ts or is_reply or is_bot or message.get("subtype"):
                stats["skipped"] += 1
                continue

            text = str(message.get("text", ""))
            parsed = parse_schedule_message(text)
            if not parsed:
                stats["skipped"] += 1
                continue
            active_timestamps.add(message_ts)
            source_hash = hashlib.sha256(text.strip().encode()).hexdigest()
            event = ScheduleEvent.objects.filter(
                slack_channel_id=channel_id,
                slack_message_ts=message_ts,
            ).first()
            created = event is None
            if created:
                event = ScheduleEvent(
                    source_type=ScheduleEvent.SourceType.SLACK,
                    slack_channel_id=channel_id,
                    slack_message_ts=message_ts,
                )
            preserve_manual_category = (
                not created
                and event.todo_category_manual
                and parsed.item_type == ScheduleEvent.ItemType.TODO
            )
            todo_category_id = (
                event.todo_category_id
                if preserve_manual_category
                else parsed.todo_category_id
            )
            todo_category_manual = bool(preserve_manual_category)
            changed = created or any(
                (
                    event.title != parsed.title,
                    event.item_type != parsed.item_type,
                    event.todo_category_id != todo_category_id,
                    event.todo_category_manual != todo_category_manual,
                    event.starts_at != parsed.starts_at,
                    event.ends_at != parsed.ends_at,
                    event.all_day != parsed.all_day,
                    event.notes != parsed.notes,
                    event.source_hash != source_hash,
                )
            )
            if not changed:
                stats["unchanged"] += 1
                continue
            event.title = parsed.title
            event.item_type = parsed.item_type
            event.todo_category_id = todo_category_id
            event.todo_category_manual = todo_category_manual
            event.starts_at = parsed.starts_at
            event.ends_at = parsed.ends_at
            event.all_day = parsed.all_day
            event.notes = parsed.notes
            event.source_hash = source_hash
            event.full_clean()
            event.save()
            stats["created" if created else "updated"] += 1

        stale = ScheduleEvent.objects.filter(
            source_type=ScheduleEvent.SourceType.SLACK,
            slack_channel_id=channel_id,
        )
        if active_timestamps:
            stale = stale.exclude(slack_message_ts__in=active_timestamps)
        deleted, _ = stale.delete()
        stats["deleted"] = deleted

    return stats
