import json
import os
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from dashboard.models import (
    ContentRun,
    KnowledgeConsumptionState,
    KnowledgeItem,
    UserRunState,
)


def timestamp_range(queryset) -> dict[str, str | int | None]:
    values = queryset.aggregate(
        true_count=Count("id"),
        earliest_updated_at=Min("updated_at"),
        latest_updated_at=Max("updated_at"),
    )
    return {
        "true_count": values["true_count"],
        "earliest_updated_at": (
            values["earliest_updated_at"].isoformat()
            if values["earliest_updated_at"]
            else None
        ),
        "latest_updated_at": (
            values["latest_updated_at"].isoformat()
            if values["latest_updated_at"]
            else None
        ),
    }


def canonical_timestamp_range(queryset, field: str) -> dict[str, str | int | None]:
    values = queryset.aggregate(
        true_count=Count("id"),
        earliest=Min(field),
        latest=Max(field),
    )
    return {
        "true_count": values["true_count"],
        "earliest_at": values["earliest"].isoformat() if values["earliest"] else None,
        "latest_at": values["latest"].isoformat() if values["latest"] else None,
    }


def write_private_json(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class Command(BaseCommand):
    help = "지식 이관 전 상태를 원문 없이 읽기 전용 JSON으로 감사합니다."

    def add_arguments(self, parser):
        parser.add_argument("--output", type=Path)

    def handle(self, *args, **options):
        content_runs = ContentRun.objects.all()
        knowledge_items = KnowledgeItem.objects.all()
        user_states = UserRunState.objects.all()
        consumption_states = KnowledgeConsumptionState.objects.all()

        orphan_run_ids = list(
            content_runs.filter(knowledge_item__isnull=True)
            .order_by("id")
            .values_list("id", flat=True)
        )
        hidden_mismatches = list(
            knowledge_items.filter(content_run__isnull=False)
            .exclude(
                Q(hidden_at__isnull=True, content_run__hidden_at__isnull=True)
                | Q(hidden_at__isnull=False, content_run__hidden_at__isnull=False)
            )
            .order_by("content_run_id", "id")
            .values("content_run_id", "id")
        )
        session_counts = list(
            user_states.values("run_id")
            .annotate(session_count=Count("session_key", distinct=True))
            .filter(session_count__gt=1)
            .order_by("run_id")
        )
        note_counts = list(
            user_states.values("run_id")
            .annotate(
                empty_note_count=Count("id", filter=Q(note="")),
                non_empty_note_count=Count("id", filter=~Q(note="")),
                distinct_non_empty_note_count=Count(
                    "note", distinct=True, filter=~Q(note="")
                ),
            )
            .order_by("run_id")
        )

        payload = {
            "schema_version": 1,
            "status": "pass",
            "generated_at": timezone.now().isoformat(),
            "counts": {
                "content_runs": {
                    "total": content_runs.count(),
                    "visible": content_runs.filter(hidden_at__isnull=True).count(),
                    "hidden": content_runs.filter(hidden_at__isnull=False).count(),
                },
                "knowledge_items": {
                    "total": knowledge_items.count(),
                    "visible": knowledge_items.filter(hidden_at__isnull=True).count(),
                    "hidden": knowledge_items.filter(hidden_at__isnull=False).count(),
                },
                "user_run_states": {
                    "total": user_states.count(),
                    "for_visible_runs": user_states.filter(
                        run__hidden_at__isnull=True
                    ).count(),
                    "for_hidden_runs": user_states.filter(
                        run__hidden_at__isnull=False
                    ).count(),
                },
                "knowledge_consumption_states": {
                    "total": consumption_states.count(),
                    "for_visible_items": consumption_states.filter(
                        knowledge_item__hidden_at__isnull=True
                    ).count(),
                    "for_hidden_items": consumption_states.filter(
                        knowledge_item__hidden_at__isnull=False
                    ).count(),
                },
            },
            "legacy_orphans": {
                "count": len(orphan_run_ids),
                "content_run_ids": orphan_run_ids,
            },
            "hidden_pair_mismatches": {
                "count": len(hidden_mismatches),
                "content_run_ids": [row["content_run_id"] for row in hidden_mismatches],
                "knowledge_item_ids": [row["id"] for row in hidden_mismatches],
            },
            "multiple_session_runs": {
                "count": len(session_counts),
                "content_run_ids": [row["run_id"] for row in session_counts],
                "session_counts": [
                    {"content_run_id": row["run_id"], "count": row["session_count"]}
                    for row in session_counts
                ],
            },
            "note_conflicts": {
                "empty_note_state_count": sum(row["empty_note_count"] for row in note_counts),
                "non_empty_note_state_count": sum(
                    row["non_empty_note_count"] for row in note_counts
                ),
                "empty_and_non_empty_content_run_ids": [
                    row["run_id"]
                    for row in note_counts
                    if row["empty_note_count"] and row["non_empty_note_count"]
                ],
                "distinct_non_empty_content_run_ids": [
                    row["run_id"]
                    for row in note_counts
                    if row["distinct_non_empty_note_count"] > 1
                ],
            },
            "state_flags": {
                "bookmarked": timestamp_range(user_states.filter(bookmarked=True)),
                "completed": timestamp_range(user_states.filter(completed=True)),
            },
            "canonical_state_flags": {
                field.removesuffix("_at"): canonical_timestamp_range(
                    consumption_states.filter(**{f"{field}__isnull": False}), field
                )
                for field in (
                    "read_at",
                    "bookmarked_at",
                    "completed_at",
                    "archived_at",
                )
            },
            "redaction": {
                "raw_notes_recorded": False,
                "session_keys_recorded": False,
                "database_row_values_recorded": False,
            },
        }
        content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if options["output"]:
            write_private_json(options["output"], content)
        self.stdout.write(content, ending="")
