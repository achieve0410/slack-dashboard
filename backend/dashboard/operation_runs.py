import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone


OPERATION_KINDS = frozenset({"sync", "classify", "quiz", "tagging"})
OPERATION_STATUSES = frozenset({"running", "success", "failed", "skipped"})
OPERATION_ERROR_CODES = frozenset(
    {
        "",
        "lock_contended",
        "configuration_error",
        "source_unavailable",
        "authentication_failed",
        "api_error",
        "database_error",
        "classifier_initialization",
        "classifier_inference",
        "classifier_validation",
        "quiz_inventory",
        "quiz_generation",
        "quiz_validation",
        "tag_inventory",
        "tag_generation",
        "tag_artifact",
        "tag_validation",
        "tag_stale_inventory",
        "unexpected_error",
    }
)
SUMMARY_KEYS = frozenset(
    {
        "selected",
        "classified",
        "needs_review",
        "stale",
        "transient_failure",
        "missing_usage",
        "category_created",
        "category_reused",
        "category_existing",
        "usage_reports",
        "channels_synced",
        "runs_imported",
        "runs_deleted",
        "questions_imported",
        "schedule_created",
        "schedule_updated",
        "schedule_deleted",
        "schedule_skipped",
        "backlog_pending",
        "backlog_review",
        "quiz_candidates",
        "quiz_published",
        "quiz_quarantined",
        "quiz_failed",
        "quiz_dry_run",
        "tag_inventory",
        "tag_candidates",
        "tag_assigned_items",
        "tag_assignments",
        "tag_reviewed_items",
        "tag_failed_items",
        "tag_dry_run",
        "tag_published",
        "tag_stale_inventory",
        "elapsed_ms",
    }
)
SUMMARY_MAX_BYTES = 4096
RETENTION = timedelta(days=90)
FRESHNESS_THRESHOLDS = {
    "sync": timedelta(minutes=30),
    "classify": timedelta(hours=36),
    "quiz": timedelta(days=7),
    "tagging": timedelta(hours=36),
}


@dataclass(frozen=True)
class OperationAttempt:
    kind: str
    started_at: datetime
    run_id: int | None


def _operation_model():
    return apps.get_model("dashboard", "OperationRun")


def validate_operation_details(
    kind: str,
    status: str,
    error_code: str,
    summary: dict,
) -> None:
    errors = {}
    if kind not in OPERATION_KINDS:
        errors["kind"] = "지원하지 않는 작업 종류입니다."
    if status not in OPERATION_STATUSES:
        errors["status"] = "지원하지 않는 작업 상태입니다."
    if error_code not in OPERATION_ERROR_CODES:
        errors["error_code"] = "지원하지 않는 오류 코드입니다."
    if not isinstance(summary, dict):
        errors["summary"] = "작업 요약은 JSON 객체여야 합니다."
    else:
        unknown_keys = sorted(set(summary) - SUMMARY_KEYS)
        if unknown_keys:
            errors["summary"] = "지원하지 않는 요약 필드가 있습니다."
        elif any(
            isinstance(value, str)
            or not isinstance(value, (int, float, bool, type(None)))
            for value in summary.values()
        ):
            errors["summary"] = "작업 요약에는 숫자, boolean, null만 허용됩니다."
        else:
            try:
                serialized_size = len(
                    json.dumps(
                        summary,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                )
            except (OverflowError, TypeError, ValueError):
                errors["summary"] = "작업 요약을 안전한 JSON으로 직렬화할 수 없습니다."
            else:
                if serialized_size > SUMMARY_MAX_BYTES:
                    errors["summary"] = "작업 요약은 4KB 이하여야 합니다."
    if errors:
        raise ValidationError(errors)


def _fallback_log(logger, *, kind: str, status: str, error_code: str, error) -> None:
    logger.warning(
        "operation_log_fallback kind=%s status=%s error_code=%s exception=%s",
        kind if kind in OPERATION_KINDS else "invalid",
        status if status in OPERATION_STATUSES else "invalid",
        error_code if error_code in OPERATION_ERROR_CODES else "invalid",
        type(error).__name__,
    )


def start_operation(
    kind: str,
    *,
    logger=None,
    model=None,
    now: datetime | None = None,
) -> OperationAttempt:
    logger = logger or logging.getLogger(__name__)
    started_at = now or timezone.now()
    try:
        validate_operation_details(kind, "running", "", {})
        operation_model = model or _operation_model()
        run = operation_model.objects.create(
            kind=kind,
            status="running",
            error_code="",
            summary={},
            started_at=started_at,
        )
    except Exception as error:
        _fallback_log(
            logger,
            kind=kind,
            status="running",
            error_code="database_error",
            error=error,
        )
        return OperationAttempt(kind=kind, started_at=started_at, run_id=None)
    return OperationAttempt(kind=kind, started_at=started_at, run_id=run.pk)


def finish_operation(
    attempt: OperationAttempt,
    status: str,
    *,
    error_code: str = "",
    summary: dict | None = None,
    logger=None,
    model=None,
    now: datetime | None = None,
) -> bool:
    logger = logger or logging.getLogger(__name__)
    summary = summary or {}
    try:
        validate_operation_details(attempt.kind, status, error_code, summary)
        operation_model = model or _operation_model()
        if attempt.run_id is None:
            raise RuntimeError("operation start was not persisted")
        updated = operation_model.objects.filter(pk=attempt.run_id).update(
            status=status,
            error_code=error_code,
            summary=summary,
            finished_at=now or timezone.now(),
        )
        if updated != 1:
            raise RuntimeError("operation row is missing")
    except Exception as error:
        _fallback_log(
            logger,
            kind=attempt.kind,
            status=status,
            error_code=error_code or "database_error",
            error=error,
        )
        return False
    return True


def prune_operation_runs(*, logger=None, model=None, now: datetime | None = None) -> int:
    logger = logger or logging.getLogger(__name__)
    cutoff = (now or timezone.now()) - RETENTION
    try:
        operation_model = model or _operation_model()
        deleted, _ = operation_model.objects.filter(
            Q(finished_at__lt=cutoff)
            | Q(finished_at__isnull=True, started_at__lt=cutoff)
        ).delete()
    except Exception as error:
        _fallback_log(
            logger,
            kind="sync",
            status="skipped",
            error_code="database_error",
            error=error,
        )
        return 0
    return deleted


def freshness_state(
    kind: str,
    last_success: datetime | None,
    *,
    now: datetime | None = None,
) -> dict:
    try:
        threshold = FRESHNESS_THRESHOLDS[kind]
    except KeyError as error:
        raise ValueError("지원하지 않는 작업 종류입니다.") from error
    checked_at = now or timezone.now()
    stale = last_success is None or checked_at - last_success > threshold
    return {
        "last_success": last_success,
        "stale": stale,
        "threshold_seconds": int(threshold.total_seconds()),
    }


def operations_summary(*, now: datetime | None = None) -> dict:
    operation_model = _operation_model()
    knowledge_model = apps.get_model("dashboard", "KnowledgeItem")
    checked_at = now or timezone.now()
    visible = knowledge_model.objects.filter(hidden_at__isnull=True).filter(
        Q(content_run__isnull=True) | Q(content_run__hidden_at__isnull=True)
    )
    backlog = {
        "pending": visible.filter(status="pending").count(),
        "review": visible.filter(status="needs_review").count(),
    }
    result = {"backlog": backlog}
    for kind in sorted(OPERATION_KINDS):
        attempts = operation_model.objects.filter(kind=kind).order_by("-started_at", "-pk")
        last_attempt = attempts.first()
        last_success = attempts.filter(status="success").first()
        result[kind] = {
            "last_attempt": last_attempt,
            **freshness_state(
                kind,
                (last_success.finished_at or last_success.started_at)
                if last_success
                else None,
                now=checked_at,
            ),
        }
    return result
