from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, override_settings

from .schedule_groups import AgendaGroup, agenda_group, agenda_group_counts


SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 7, 17, 12, tzinfo=SEOUL)


def event(
    item_type: str,
    starts_at: datetime | None,
    *,
    ends_at: datetime | None = None,
    completed: bool = False,
):
    return SimpleNamespace(
        item_type=item_type,
        starts_at=starts_at,
        ends_at=ends_at,
        completed=completed,
    )


@override_settings(TIME_ZONE="Asia/Seoul", USE_TZ=True)
class ScheduleGroupTests(SimpleTestCase):
    def test_priority_truth_table_is_exhaustive(self):
        fixtures = (
            (event("schedule", None, completed=True), AgendaGroup.COMPLETED),
            (
                event(
                    "schedule",
                    datetime(2026, 7, 16, 23, tzinfo=SEOUL),
                    ends_at=datetime(2026, 7, 17, 1, tzinfo=SEOUL),
                ),
                AgendaGroup.TODAY,
            ),
            (
                event("todo", datetime(2026, 7, 17, 23, 59, 59, tzinfo=SEOUL)),
                AgendaGroup.TODAY,
            ),
            (
                event("todo", datetime(2026, 7, 16, 23, 59, 59, tzinfo=SEOUL)),
                AgendaGroup.OVERDUE_TODO,
            ),
            (
                event(
                    "schedule",
                    datetime(2026, 7, 16, 22, tzinfo=SEOUL),
                    ends_at=datetime(2026, 7, 16, 23, 59, 59, tzinfo=SEOUL),
                ),
                AgendaGroup.PAST_SCHEDULE,
            ),
            (
                event("schedule", datetime(2026, 7, 16, 23, tzinfo=SEOUL)),
                AgendaGroup.PAST_SCHEDULE,
            ),
            (
                event("schedule", datetime(2026, 7, 18, 0, tzinfo=SEOUL)),
                AgendaGroup.UPCOMING,
            ),
            (event("todo", None), AgendaGroup.UNDATED),
        )

        for schedule_event, expected in fixtures:
            with self.subTest(expected=expected):
                self.assertEqual(agenda_group(schedule_event, now=NOW), expected)

        counts = agenda_group_counts((fixture[0] for fixture in fixtures), now=NOW)
        self.assertEqual(sum(counts.values()), len(fixtures))
        self.assertEqual(set(counts), {group.value for group in AgendaGroup})

    def test_completed_wins_over_type_and_date(self):
        completed_undated_todo = event("todo", None, completed=True)
        completed_future_schedule = event(
            "schedule",
            datetime(2026, 7, 20, tzinfo=SEOUL),
            completed=True,
        )

        self.assertEqual(
            agenda_group(completed_undated_todo, now=NOW),
            AgendaGroup.COMPLETED,
        )
        self.assertEqual(
            agenda_group(completed_future_schedule, now=NOW),
            AgendaGroup.COMPLETED,
        )

    def test_today_boundaries_use_asia_seoul(self):
        just_before = event(
            "todo",
            datetime(2026, 7, 16, 23, 59, 59, tzinfo=SEOUL),
        )
        today_start = event("todo", datetime(2026, 7, 17, 0, tzinfo=SEOUL))
        today_end = event(
            "todo",
            datetime(2026, 7, 17, 23, 59, 59, tzinfo=SEOUL),
        )
        tomorrow_start = event("todo", datetime(2026, 7, 18, 0, tzinfo=SEOUL))

        self.assertEqual(agenda_group(just_before, now=NOW), AgendaGroup.OVERDUE_TODO)
        self.assertEqual(agenda_group(today_start, now=NOW), AgendaGroup.TODAY)
        self.assertEqual(agenda_group(today_end, now=NOW), AgendaGroup.TODAY)
        self.assertEqual(agenda_group(tomorrow_start, now=NOW), AgendaGroup.UPCOMING)

    def test_invalid_incomplete_schedule_fails_closed(self):
        with self.assertRaisesMessage(ValueError, "유효한 schedule 또는 todo"):
            agenda_group(event("schedule", None), now=NOW)
