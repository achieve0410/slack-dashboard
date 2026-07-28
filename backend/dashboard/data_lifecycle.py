import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from django.core.management import call_command
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    ContentRun,
    CronJob,
    KnowledgeItem,
    QuizQuestion,
    QuizSession,
)


class DataLifecycleError(ValueError):
    pass


@dataclass(frozen=True)
class LifecycleResult:
    sources: int = 0
    knowledge_items: int = 0
    content_runs: int = 0
    quiz_questions: int = 0
    quiz_sessions: int = 0

    def payload(self) -> dict:
        return {
            "sources": self.sources,
            "knowledge_items": self.knowledge_items,
            "content_runs": self.content_runs,
            "quiz_questions": self.quiz_questions,
            "quiz_sessions": self.quiz_sessions,
        }


def disconnect_slack_source(
    channel_id: str,
    *,
    purge: bool = False,
) -> LifecycleResult:
    channel_id = str(channel_id or "").strip()
    if not channel_id or len(channel_id) > 50:
        raise DataLifecycleError("Slack 채널 ID를 확인해주세요.")
    try:
        job = CronJob.objects.get(external_id=f"channel:{channel_id}")
    except CronJob.DoesNotExist as error:
        raise DataLifecycleError("연결된 Slack 소스를 찾을 수 없습니다.") from error
    if not purge:
        job.enabled = False
        job.state = "disconnected"
        job.disconnected_at = timezone.now()
        job.save(
            update_fields=[
                "enabled",
                "state",
                "disconnected_at",
                "updated_at",
            ]
        )
        return LifecycleResult(sources=1)
    with transaction.atomic():
        item_ids = list(
            KnowledgeItem.objects.filter(content_run__job=job).values_list(
                "pk",
                flat=True,
            )
        )
        run_count = ContentRun.objects.filter(job=job).count()
        question_ids = list(
            QuizQuestion.objects.filter(
                knowledge_item_id__in=item_ids
            ).values_list("pk", flat=True)
        )
        session_ids = list(
            QuizSession.objects.filter(
                items__question_id__in=question_ids
            )
            .distinct()
            .values_list("pk", flat=True)
        )
        if session_ids:
            QuizSession.objects.filter(pk__in=session_ids).delete()
        if question_ids:
            QuizQuestion.objects.filter(
                superseded_by_id__in=question_ids
            ).update(superseded_by=None)
            QuizQuestion.objects.filter(pk__in=question_ids).delete()
        job.delete()
    return LifecycleResult(
        sources=1,
        knowledge_items=len(item_ids),
        content_runs=run_count,
        quiz_questions=len(question_ids),
        quiz_sessions=len(session_ids),
    )


def prune_dashboard_data(
    *,
    days: int,
    hard_delete: bool = False,
    now=None,
) -> LifecycleResult:
    if not 1 <= days <= 3650:
        raise DataLifecycleError("보존 기간은 1~3650일이어야 합니다.")
    cutoff = (now or timezone.now()) - timedelta(days=days)
    queryset = KnowledgeItem.objects.filter(generated_at__lt=cutoff).filter(
        Q(content_run__isnull=True) | Q(content_run__job__external_id__startswith="channel:")
    )
    item_ids = list(queryset.values_list("pk", flat=True))
    run_ids = list(
        queryset.exclude(content_run_id__isnull=True).values_list(
            "content_run_id",
            flat=True,
        )
    )
    if not item_ids:
        return LifecycleResult()
    if not hard_delete:
        hidden_at = now or timezone.now()
        KnowledgeItem.objects.filter(pk__in=item_ids).update(hidden_at=hidden_at)
        ContentRun.objects.filter(pk__in=run_ids).update(hidden_at=hidden_at)
        return LifecycleResult(
            knowledge_items=len(item_ids),
            content_runs=len(run_ids),
        )
    with transaction.atomic():
        question_ids = list(
            QuizQuestion.objects.filter(
                knowledge_item_id__in=item_ids
            ).values_list("pk", flat=True)
        )
        session_ids = list(
            QuizSession.objects.filter(
                items__question_id__in=question_ids
            )
            .distinct()
            .values_list("pk", flat=True)
        )
        if session_ids:
            QuizSession.objects.filter(pk__in=session_ids).delete()
        if question_ids:
            QuizQuestion.objects.filter(
                superseded_by_id__in=question_ids
            ).update(superseded_by=None)
            QuizQuestion.objects.filter(pk__in=question_ids).delete()
        KnowledgeItem.objects.filter(pk__in=item_ids).delete()
        ContentRun.objects.filter(pk__in=run_ids).delete()
    return LifecycleResult(
        knowledge_items=len(item_ids),
        content_runs=len(run_ids),
        quiz_questions=len(question_ids),
        quiz_sessions=len(session_ids),
    )


def create_dashboard_backup(*, filename: str = "") -> Path:
    root = backup_root()
    root.mkdir(parents=True, exist_ok=True)
    if filename:
        candidate = Path(filename)
        if candidate.name != filename or not filename.endswith(".json"):
            raise DataLifecycleError("백업 파일명은 경로가 없는 .json 이름이어야 합니다.")
    else:
        filename = timezone.localtime().strftime("dashboard-%Y%m%d-%H%M%S.json")
    target = (root / filename).resolve()
    if target.parent != root:
        raise DataLifecycleError("백업 경로가 허용된 디렉터리를 벗어났습니다.")
    temporary = target.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        call_command(
            "dumpdata",
            indent=2,
            exclude=[
                "admin.logentry",
                "auth.permission",
                "contenttypes",
                "sessions",
            ],
            stdout=stream,
        )
    temporary.replace(target)
    return target


def lifecycle_status() -> dict:
    try:
        retention_days = int(os.getenv("DASHBOARD_RETENTION_DAYS", "0"))
    except ValueError:
        retention_days = -1
    root = backup_root()
    latest = None
    if root.exists():
        backups = sorted(
            (
                path
                for path in root.glob("dashboard-*.json")
                if path.is_file()
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if backups:
            latest = {
                "filename": backups[0].name,
                "created_at": datetime.fromtimestamp(
                    backups[0].stat().st_mtime,
                    tz=timezone.get_current_timezone(),
                ),
            }
    return {
        "retention_days": retention_days,
        "retention_enabled": retention_days > 0,
        "latest_backup": latest,
    }


def backup_root() -> Path:
    from django.conf import settings

    configured = os.getenv("DASHBOARD_BACKUP_DIR", "").strip() or str(
        settings.PROJECT_ROOT / "db" / "backups"
    )
    return Path(os.path.expanduser(configured)).resolve()
