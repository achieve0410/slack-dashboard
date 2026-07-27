import json
import logging
import re
from collections import Counter
from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from .access import dashboard_staff_required, staff_required_response
from .knowledge_actions import (
    UNDO_WINDOW,
    KnowledgeActionError,
    canonical_action_parameters,
    create_snapshot,
    execute_snapshot,
    normalize_target_ids,
    restore_items,
    undo_hide_snapshot,
)
from .knowledge_filters import (
    KnowledgeFilterError,
    apply_knowledge_filters,
    parse_knowledge_filters,
    parse_saved_filter_values,
)
from .knowledge_tags import (
    active_tag_snapshot_id,
    attach_tag_labels,
    item_tag_labels,
    replace_item_tags,
)
from .models import (
    BulkSelectionSnapshot,
    Category,
    ContentRun,
    CronJob,
    KnowledgeConsumptionState,
    KnowledgeItem,
    OperationRun,
    SavedKnowledgeView,
    ScheduleCategory,
    ScheduleEvent,
    UserResponse,
)
from .operation_runs import operations_summary
from .quiz_sessions import (
    QuizApiError,
    answer_item,
    catalog_payload,
    create_session,
    result_payload,
    session_history_payload,
    session_payload,
)
from .quiz_review import review_payload, update_manual_wrong_note
from .review import approve_knowledge_items
from .schedule_groups import agenda_group, agenda_group_counts
from .schedule_sync import infer_todo_category, reclassify_automatic_todos


logger = logging.getLogger(__name__)


def session_key(request: HttpRequest, *, create: bool = False) -> str:
    if create and not request.session.session_key:
        request.session.create()
    return request.session.session_key or ""


def parse_json(request: HttpRequest) -> dict:
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        raise ValueError("올바른 JSON 요청이 아닙니다.") from exc


CONSUMPTION_BOOLEAN_FIELDS = {
    "read": "read_at",
    "bookmarked": "bookmarked_at",
    "completed": "completed_at",
    "archived": "archived_at",
}


def state_payload(state: KnowledgeConsumptionState | None) -> dict:
    return {
        **{
            field: bool(state and getattr(state, timestamp_field))
            for field, timestamp_field in CONSUMPTION_BOOLEAN_FIELDS.items()
        },
        **{
            timestamp_field: getattr(state, timestamp_field) if state else None
            for timestamp_field in CONSUMPTION_BOOLEAN_FIELDS.values()
        },
        "note": state.note if state else "",
        "created_at": state.created_at if state else None,
        "updated_at": state.updated_at if state else None,
    }


def state_for_item(item: KnowledgeItem | None) -> KnowledgeConsumptionState | None:
    return getattr(item, "consumption_state", None) if item else None


def validate_consumption_patch(data) -> str | None:
    if not isinstance(data, dict):
        return "요청 본문은 JSON 객체여야 합니다."
    unknown = set(data) - {*CONSUMPTION_BOOLEAN_FIELDS, "note"}
    if unknown:
        return f"지원하지 않는 필드입니다: {', '.join(sorted(unknown))}"
    if any(type(data[field]) is not bool for field in CONSUMPTION_BOOLEAN_FIELDS if field in data):
        return "상태 필드는 JSON true 또는 false여야 합니다."
    if "note" in data:
        if not isinstance(data["note"], str):
            return "note는 문자열이어야 합니다."
        if len(data["note"]) > 5000:
            return "note는 5,000자 이하여야 합니다."
    return None


def update_consumption_state(item_id: int, data: dict) -> JsonResponse:
    with transaction.atomic():
        item = (
            visible_knowledge_items()
            .select_for_update()
            .filter(pk=item_id)
            .first()
        )
        if not item:
            return JsonResponse({"error": "지식 항목을 찾을 수 없습니다."}, status=404)
        state = (
            KnowledgeConsumptionState.objects.select_for_update()
            .filter(knowledge_item_id=item.pk)
            .first()
        )
        changes = {}
        transition_time = None
        for field, timestamp_field in CONSUMPTION_BOOLEAN_FIELDS.items():
            if field not in data:
                continue
            current = bool(state and getattr(state, timestamp_field))
            if data[field] != current:
                if data[field] and transition_time is None:
                    transition_time = timezone.now()
                changes[timestamp_field] = transition_time if data[field] else None
        if "note" in data and data["note"] != (state.note if state else ""):
            changes["note"] = data["note"]
        if not changes:
            return JsonResponse(state_payload(state))

        if state is None:
            state = KnowledgeConsumptionState(knowledge_item=item, **changes)
            state.save()
        else:
            for field, value in changes.items():
                setattr(state, field, value)
            state.save(update_fields=[*changes, "updated_at"])
        return JsonResponse(state_payload(state))


def job_payload(job: CronJob) -> dict:
    return {
        "id": job.id,
        "external_id": job.external_id,
        "name": job.name,
        "category": job.category,
        "category_label": job.get_category_display(),
        "schedule": job.schedule,
        "timezone": job.timezone,
        "enabled": job.enabled,
        "state": job.state,
        "last_status": job.last_status,
        "last_error": job.last_error,
        "last_run_at": job.last_run_at,
        "next_run_at": job.next_run_at,
        "thread_ts": job.thread_ts,
        "model_name": job.model_name,
    }


def run_payload(
    run: ContentRun,
    current_session: str,
    *,
    detail: bool = False,
    tag_snapshot_id: int | None = None,
) -> dict:
    knowledge_item = getattr(run, "knowledge_item", None)
    payload = {
        "id": run.id,
        "title": run.title,
        "status": run.status,
        "generated_at": run.generated_at,
        "excerpt": (run.body or run.error).replace("\n", " ")[:220],
        "job": {
            "id": run.job_id,
            "external_id": run.job.external_id,
            "name": run.job.name,
            "category": run.job.category,
            "category_label": run.job.get_category_display(),
        },
        "state": state_payload(state_for_item(knowledge_item)),
        "citation_count": len(run.citations.all()),
    }
    if detail:
        payload.update(
            {
                "body": run.body,
                "error": run.error,
                "structured_data": run.structured_data,
                "model_name": run.model_name,
                "prompt_version": run.prompt_version,
                "citations": [
                    {"title": item.title, "url": item.url, "publisher": item.publisher}
                    for item in run.citations.all()
                ],
                "responses": [
                    {
                        "id": item.id,
                        "question_key": item.question_key,
                        "answer": item.answer,
                        "feedback": item.feedback,
                        "score": item.score,
                        "created_at": item.created_at,
                    }
                    for item in run.responses.all()
                    if item.session_key == current_session
                ],
            }
        )
        if knowledge_item and knowledge_item.hidden_at:
            knowledge_item = None
        if knowledge_item:
            attach_tag_labels([knowledge_item], snapshot_id=tag_snapshot_id)
        payload["knowledge_item"] = (
            knowledge_detail_payload(knowledge_item) if knowledge_item else None
        )
    return payload


def run_queryset(current_session: str, *, include_archived: bool = False):
    queryset = (
        ContentRun.objects.filter(hidden_at__isnull=True).select_related(
            "job",
            "knowledge_item",
            "knowledge_item__category",
            "knowledge_item__consumption_state",
            "knowledge_item__reviewed_by",
        )
        .prefetch_related("citations", "responses")
    )
    if include_archived:
        return queryset
    return queryset.filter(
        Q(knowledge_item__isnull=True)
        | Q(knowledge_item__consumption_state__archived_at__isnull=True)
    )


def visible_knowledge_items():
    return KnowledgeItem.objects.filter(hidden_at__isnull=True).filter(
        Q(content_run__isnull=True) | Q(content_run__hidden_at__isnull=True)
    )


def active_knowledge_items():
    return visible_knowledge_items().filter(
        consumption_state__archived_at__isnull=True
    )


def category_payload(category: Category | None) -> dict | None:
    if not category:
        return None
    return {
        "id": category.id,
        "name": category.name,
        "path": category.path,
        "depth": category.depth,
        "parent_id": category.parent_id,
    }


def knowledge_detail_url(item: KnowledgeItem) -> str:
    if item.source_type == KnowledgeItem.SourceType.CRON:
        return f"/runs/{item.content_run_id}"
    return f"/knowledge/{item.id}"


def knowledge_card_payload(item: KnowledgeItem) -> dict:
    summary_text = item.summary
    if not summary_text and item.content_run_id:
        summary_text = item.content_run.body
    elif not summary_text:
        summary_text = item.answer
    payload = {
        "id": item.id,
        "title": item.title,
        "summary": summary_text.replace("\n", " ")[:600],
        "generated_at": item.generated_at,
        "classified_at": item.classified_at,
        "classification_stale_at": item.classification_stale_at,
        "source_type": item.source_type,
        "source_label": item.get_source_type_display(),
        "status": item.status,
        "status_label": item.get_status_display(),
        "category": category_payload(item.category),
        "category_path": item.category.path if item.category else "",
        "tags": item_tag_labels(item),
        "detail_url": knowledge_detail_url(item),
        "content_run_id": item.content_run_id,
        "state": state_payload(state_for_item(item)),
    }
    if item.source_type == KnowledgeItem.SourceType.SLACK_QA:
        payload.update(
            {
                "question_excerpt": item.question.replace("\n", " ")[:220],
                "has_answer": bool(item.answer),
            }
        )
    return payload


def knowledge_detail_payload(item: KnowledgeItem) -> dict:
    payload = {
        "id": item.id,
        "title": item.title,
        "summary": item.summary,
        "generated_at": item.generated_at,
        "classified_at": item.classified_at,
        "classification_stale_at": item.classification_stale_at,
        "source_type": item.source_type,
        "source_label": item.get_source_type_display(),
        "status": item.status,
        "status_label": item.get_status_display(),
        "category": category_payload(item.category),
        "category_path": item.category.path if item.category else "",
        "tags": item_tag_labels(item),
        "detail_url": knowledge_detail_url(item),
        "classification_model": item.classification_model,
        "classification_confidence": item.classification_confidence,
        "classification_reason": item.classification_reason,
        "reviewed_by": (
            {
                "id": item.reviewed_by_id,
                "username": item.reviewed_by.get_username(),
            }
            if item.reviewed_by_id
            else None
        ),
        "reviewed_at": item.reviewed_at,
        "state": state_payload(state_for_item(item)),
    }
    if item.source_type == KnowledgeItem.SourceType.SLACK_QA:
        payload.update(
            {
                "question": item.question,
                "answer": item.answer,
                "slack": {
                    "channel_id": item.slack_channel_id,
                    "thread_ts": item.slack_thread_ts,
                    "source_url": item.slack_source_url,
                },
            }
        )
    else:
        payload["content_run_id"] = item.content_run_id
    return payload


def knowledge_queryset():
    return (
        knowledge_base_queryset()
        .filter(consumption_state__archived_at__isnull=True)
        .order_by("-generated_at", "-id")
    )


def knowledge_base_queryset():
    return visible_knowledge_items().select_related(
        "category",
        "consumption_state",
        "content_run",
        "content_run__job",
    )


def knowledge_filter_error(error: KnowledgeFilterError) -> JsonResponse:
    return JsonResponse(
        {"error": str(error), "code": error.code},
        status=error.status,
    )


def knowledge_action_error(error: KnowledgeActionError) -> JsonResponse:
    return JsonResponse(
        {"error": error.message, "code": error.code},
        status=error.status,
    )


def quiz_error(error: QuizApiError) -> JsonResponse:
    return JsonResponse(error.response_payload(), status=error.status)


def operation_run_payload(run: OperationRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "kind": run.kind,
        "status": run.status,
        "error_code": run.error_code,
        "summary": run.summary,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def operation_status_payload() -> tuple[dict, dict]:
    raw = operations_summary()
    backlog = {
        **raw["backlog"],
        "total": raw["backlog"]["pending"] + raw["backlog"]["review"],
    }
    operations = {}
    schedule_labels = {"sync": "15분마다", "tagging": "매일 02:00", "classify": "매일 03:30"}
    for kind in ("sync", "tagging", "classify"):
        state = raw[kind]
        operations[kind] = {
            "last_attempt": operation_run_payload(state["last_attempt"]),
            "last_success_at": state["last_success"],
            "stale": state["stale"],
            "threshold_seconds": state["threshold_seconds"],
            "schedule_label": schedule_labels[kind],
        }
    return operations, backlog


def search_query(request: HttpRequest, *, required: bool = False) -> str | JsonResponse:
    query = request.GET.get("q", "").strip()
    if required and not query:
        return JsonResponse({"error": "검색어를 입력해주세요."}, status=400)
    if len(query) > 200:
        return JsonResponse({"error": "검색어는 200자 이하여야 합니다."}, status=400)
    return query


def filter_knowledge_search(queryset, query: str):
    if not query:
        return queryset
    return queryset.filter(
        Q(title__icontains=query)
        | Q(summary__icontains=query)
        | Q(question__icontains=query)
        | Q(answer__icontains=query)
        | Q(content_run__body__icontains=query)
        | Q(category__path__icontains=query)
    )


def sort_knowledge(request: HttpRequest, queryset):
    sort = request.GET.get("sort", "newest")
    if sort == "newest":
        return queryset.order_by("-generated_at", "-id")
    if sort == "oldest":
        return queryset.order_by("generated_at", "id")
    return JsonResponse({"error": "sort는 newest 또는 oldest여야 합니다."}, status=400)


def schedule_payload(event: ScheduleEvent, *, agenda_group_value: str | None = None) -> dict:
    payload = {
        "id": event.id,
        "title": event.title,
        "item_type": event.item_type,
        "item_type_label": event.get_item_type_display(),
        "todo_category_id": event.todo_category_id,
        "todo_category_label": event.todo_category.name if event.todo_category_id else "",
        "todo_category_manual": event.todo_category_manual,
        "starts_at": event.starts_at,
        "ends_at": event.ends_at,
        "all_day": event.all_day,
        "notes": event.notes,
        "completed": event.completed,
        "source_type": event.source_type,
        "source_label": event.get_source_type_display(),
        "slack_channel_id": event.slack_channel_id,
        "created_at": event.created_at,
        "updated_at": event.updated_at,
    }
    if agenda_group_value is not None:
        payload["agenda_group"] = agenda_group_value
    return payload


def event_datetime(value: object, field: str):
    if value in (None, ""):
        return None
    parsed = parse_datetime(str(value))
    if not parsed:
        raise ValueError(f"{field}은 올바른 ISO 날짜·시각이어야 합니다.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def update_schedule_event(event: ScheduleEvent, data: dict) -> None:
    if "item_type" in data:
        item_type = str(data["item_type"])
        if item_type not in ScheduleEvent.ItemType.values:
            raise ValueError("item_type은 schedule 또는 todo여야 합니다.")
        event.item_type = item_type
    if "title" in data:
        event.title = str(data["title"])[:200]
    if "starts_at" in data:
        event.starts_at = event_datetime(data["starts_at"], "starts_at")
    if "ends_at" in data:
        event.ends_at = event_datetime(data["ends_at"], "ends_at")
    if "notes" in data:
        event.notes = str(data["notes"])[:5000]
    for field in ("all_day", "completed"):
        if field in data:
            if not isinstance(data[field], bool):
                raise ValueError(f"{field}은 true 또는 false여야 합니다.")
            setattr(event, field, data[field])
    if event.item_type == ScheduleEvent.ItemType.TODO:
        if "todo_category_id" in data:
            category_id = data["todo_category_id"]
            if category_id in (None, ""):
                event.todo_category = infer_todo_category(event.title)
                event.todo_category_manual = False
            else:
                try:
                    event.todo_category = ScheduleCategory.objects.get(pk=int(category_id))
                except (TypeError, ValueError, ScheduleCategory.DoesNotExist) as error:
                    raise ValueError("할 일 카테고리를 찾을 수 없습니다.") from error
                event.todo_category_manual = True
        elif not event.todo_category_manual:
            event.todo_category = infer_todo_category(event.title)
        if not event.starts_at:
            event.all_day = False
    else:
        if data.get("todo_category_id") not in (None, ""):
            raise ValueError("일정에는 할 일 카테고리를 지정할 수 없습니다.")
        event.todo_category = None
        event.todo_category_manual = False
    event.full_clean()


def schedule_category_payload(category: ScheduleCategory) -> dict:
    usage_count = (
        category.usage_count
        if hasattr(category, "usage_count")
        else category.events.count()
    )
    return {
        "id": category.id,
        "name": category.name,
        "keywords": category.keywords,
        "is_fallback": category.is_fallback,
        "usage_count": usage_count,
    }


def update_schedule_category(category: ScheduleCategory, data: dict) -> None:
    if "name" in data:
        category.name = str(data["name"])[:50]
    if "keywords" in data:
        category.keywords = data["keywords"]
    category.full_clean()


def pagination_params(request: HttpRequest) -> tuple[int, int] | JsonResponse:
    try:
        limit = int(request.GET.get("limit", "50"))
        offset = int(request.GET.get("offset", "0"))
    except ValueError:
        return JsonResponse({"error": "limit과 offset은 정수여야 합니다."}, status=400)
    if limit < 1 or limit > 100 or offset < 0:
        return JsonResponse(
            {"error": "limit은 1~100, offset은 0 이상이어야 합니다."},
            status=400,
        )
    return limit, offset


def local_day_bounds() -> tuple[datetime, datetime]:
    now = timezone.now()
    local_today = timezone.localdate(now)
    day_start = timezone.make_aware(
        datetime.combine(local_today, time.min),
        timezone.get_current_timezone(),
    )
    return day_start, day_start + timedelta(days=1)


@require_GET
def health(_request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


@require_GET
@ensure_csrf_cookie
def csrf(_request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@require_GET
def summary(request: HttpRequest) -> JsonResponse:
    current_session = session_key(request)
    jobs_qs = CronJob.objects.all()
    runs_qs = run_queryset(current_session)
    latest = list(runs_qs.filter(status=ContentRun.Status.SUCCESS)[:8])
    failures = list(runs_qs.filter(status=ContentRun.Status.FAILED)[:5])
    status_counts = Counter(jobs_qs.values_list("last_status", flat=True))
    category_counts = list(
        jobs_qs.values("category").annotate(
            total=Count("id"),
            healthy=Count("id", filter=Q(last_status="success")),
            failed=Count("id", filter=Q(last_status="error")),
        )
    )
    category_labels = dict(CronJob.Category.choices)
    completed = KnowledgeConsumptionState.objects.filter(
        completed_at__isnull=False,
        knowledge_item__hidden_at__isnull=True,
    ).count()
    responses = UserResponse.objects.filter(
        session_key=current_session,
        run__hidden_at__isnull=True,
    ).count()
    day_start, day_end = local_day_bounds()
    knowledge_items = active_knowledge_items()
    inbox_items = knowledge_items.filter(
        source_type=KnowledgeItem.SourceType.SLACK_QA,
        status__in=(
            KnowledgeItem.Status.AWAITING_ANSWER,
            KnowledgeItem.Status.PENDING,
            KnowledgeItem.Status.NEEDS_REVIEW,
        ),
    )
    latest_knowledge = list(
        knowledge_queryset().filter(status=KnowledgeItem.Status.CLASSIFIED)[:6]
    )
    tag_snapshot_id = active_tag_snapshot_id()
    attach_tag_labels(latest_knowledge, snapshot_id=tag_snapshot_id)
    today_events = ScheduleEvent.objects.filter(
        Q(starts_at__gte=day_start, starts_at__lt=day_end)
        | Q(ends_at__isnull=False, starts_at__lt=day_end, ends_at__gte=day_start)
    )
    operations, backlog = operation_status_payload()
    return JsonResponse(
        {
            "jobs": {
                "total": jobs_qs.count(),
                "enabled": jobs_qs.filter(enabled=True).count(),
                "success": status_counts.get("success", 0),
                "failed": status_counts.get("error", 0),
            },
            "progress": {"completed": completed, "responses": responses},
            "knowledge": {
                "classified": knowledge_items.filter(
                    status=KnowledgeItem.Status.CLASSIFIED
                ).count(),
                "generated_today": knowledge_items.filter(
                    generated_at__gte=day_start,
                    generated_at__lt=day_end,
                ).count(),
                "awaiting_answer": inbox_items.filter(
                    status=KnowledgeItem.Status.AWAITING_ANSWER
                ).count(),
                "pending": inbox_items.filter(status=KnowledgeItem.Status.PENDING).count(),
                "needs_review": inbox_items.filter(
                    status=KnowledgeItem.Status.NEEDS_REVIEW
                ).count(),
                "scheduled_today": today_events.count(),
            },
            "categories": [
                {**item, "label": category_labels.get(item["category"], "기타")}
                for item in category_counts
            ],
            "latest_runs": [
                run_payload(run, current_session, tag_snapshot_id=tag_snapshot_id)
                for run in latest
            ],
            "recent_failures": [
                run_payload(run, current_session, tag_snapshot_id=tag_snapshot_id)
                for run in failures
            ],
            "latest_knowledge": [
                knowledge_card_payload(item) for item in latest_knowledge
            ],
            "operations": operations,
            "backlog": backlog,
        }
    )


@require_GET
def jobs(_request: HttpRequest) -> JsonResponse:
    return JsonResponse({"results": [job_payload(job) for job in CronJob.objects.all()]})


@require_GET
def operations(request: HttpRequest) -> JsonResponse:
    pagination = pagination_params(request)
    if isinstance(pagination, JsonResponse):
        return pagination
    limit, offset = pagination
    queryset = OperationRun.objects.all()
    total = queryset.count()
    runs = list(queryset[offset : offset + limit])
    operation_status, backlog = operation_status_payload()
    return JsonResponse(
        {
            "operations": operation_status,
            "backlog": backlog,
            "count": total,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
            "results": [operation_run_payload(run) for run in runs],
        }
    )


@require_GET
def quiz_catalog(_request: HttpRequest) -> JsonResponse:
    return JsonResponse(catalog_payload())


@require_http_methods(["GET", "POST"])
def quiz_sessions(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        try:
            return JsonResponse(session_history_payload(request.GET))
        except QuizApiError as error:
            return quiz_error(error)
    try:
        data = parse_json(request)
        _session, payload = create_session(data)
    except ValueError as error:
        return JsonResponse({"error": str(error), "code": "invalid_json"}, status=400)
    except QuizApiError as error:
        return quiz_error(error)
    return JsonResponse(payload, status=201)


@require_GET
def quiz_session_detail(_request: HttpRequest, session_id) -> JsonResponse:
    try:
        return JsonResponse(session_payload(session_id))
    except QuizApiError as error:
        return quiz_error(error)


@require_http_methods(["POST"])
def quiz_session_answer(request: HttpRequest, session_id, item_id: int) -> JsonResponse:
    try:
        data = parse_json(request)
        return JsonResponse(answer_item(session_id, item_id, data))
    except ValueError as error:
        return JsonResponse({"error": str(error), "code": "invalid_json"}, status=400)
    except QuizApiError as error:
        return quiz_error(error)


@require_GET
def quiz_session_result(_request: HttpRequest, session_id) -> JsonResponse:
    try:
        return JsonResponse(result_payload(session_id))
    except QuizApiError as error:
        return quiz_error(error)


@require_GET
def quiz_review(request: HttpRequest) -> JsonResponse:
    try:
        return JsonResponse(review_payload(request.GET))
    except QuizApiError as error:
        return quiz_error(error)


@require_http_methods(["PATCH"])
def quiz_wrong_note(request: HttpRequest, question_id: int) -> JsonResponse:
    try:
        data = parse_json(request)
        return JsonResponse(update_manual_wrong_note(question_id, data))
    except ValueError as error:
        return JsonResponse({"error": str(error), "code": "invalid_json"}, status=400)
    except QuizApiError as error:
        return quiz_error(error)


@require_GET
def categories(_request: HttpRequest) -> JsonResponse:
    active_category_ids = Category.active_tree_ids()
    category_rows = list(
        Category.objects.filter(pk__in=active_category_ids).order_by("path")
    )
    direct_counts = dict(
        active_knowledge_items().filter(
            status=KnowledgeItem.Status.CLASSIFIED,
            category_id__in=active_category_ids,
        )
        .values_list("category_id")
        .annotate(total=Count("id"))
    )
    nodes = {}
    for category in category_rows:
        classified_count = sum(
            direct_counts.get(candidate.id, 0)
            for candidate in category_rows
            if candidate.path == category.path
            or candidate.path.startswith(f"{category.path}/")
        )
        nodes[category.id] = {
            **category_payload(category),
            "classified_count": classified_count,
            "children": [],
        }
    roots = []
    for category in category_rows:
        node = nodes[category.id]
        if category.parent_id in nodes:
            nodes[category.parent_id]["children"].append(node)
        else:
            roots.append(node)
    return JsonResponse({"results": roots})


@require_GET
def knowledge(request: HttpRequest) -> JsonResponse:
    pagination = pagination_params(request)
    if isinstance(pagination, JsonResponse):
        return pagination
    limit, offset = pagination
    try:
        parsed = parse_knowledge_filters(request.GET, allow_pagination=True)
    except KnowledgeFilterError as error:
        return knowledge_filter_error(error)
    tag_snapshot_id = active_tag_snapshot_id()
    queryset = apply_knowledge_filters(
        knowledge_base_queryset(),
        parsed,
        tag_snapshot_id=tag_snapshot_id,
    )

    total = queryset.count()
    items = attach_tag_labels(
        list(queryset[offset : offset + limit]),
        snapshot_id=tag_snapshot_id,
    )
    return JsonResponse(
        {
            "count": total,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
            "canonical_query": parsed.canonical_query,
            "results": [knowledge_card_payload(item) for item in items],
        }
    )


@require_GET
def search(request: HttpRequest) -> JsonResponse:
    pagination = pagination_params(request)
    if isinstance(pagination, JsonResponse):
        return pagination
    limit, offset = pagination
    try:
        parsed = parse_knowledge_filters(
            request.GET,
            required_query=True,
            allow_pagination=True,
        )
    except KnowledgeFilterError as error:
        return knowledge_filter_error(error)
    tag_snapshot_id = active_tag_snapshot_id()
    queryset = apply_knowledge_filters(
        knowledge_base_queryset(),
        parsed,
        tag_snapshot_id=tag_snapshot_id,
    )
    total = queryset.count()
    items = attach_tag_labels(
        list(queryset[offset : offset + limit]),
        snapshot_id=tag_snapshot_id,
    )
    return JsonResponse(
        {
            "count": total,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
            "canonical_query": parsed.canonical_query,
            "results": [knowledge_card_payload(item) for item in items],
        }
    )


def saved_knowledge_view_payload(view: SavedKnowledgeView) -> dict:
    return {
        "id": view.id,
        "name": view.name,
        "filters": view.canonical_filters,
        "sort": view.sort,
        "is_default": view.default_slot == 1,
        "created_at": view.created_at,
        "updated_at": view.updated_at,
    }


def saved_view_validation_error(error: ValidationError) -> JsonResponse:
    messages = error.messages
    return JsonResponse(
        {"error": "; ".join(messages), "code": "invalid_saved_view"},
        status=400,
    )


def update_saved_view_filters(
    view: SavedKnowledgeView,
    data: dict,
    *,
    creating: bool = False,
) -> None:
    if not creating and "filters" not in data and "sort" not in data:
        return
    filters = data.get("filters", view.canonical_filters)
    if not isinstance(filters, dict):
        raise KnowledgeFilterError("filters는 객체여야 합니다.")
    filters = dict(filters)
    embedded_sort = filters.pop("sort", None)
    sort = data.get("sort", embedded_sort or view.sort)
    parsed = parse_saved_filter_values(filters, sort=sort)
    view.canonical_filters = parsed.filters
    view.sort = parsed.sort


def set_saved_view_default(view: SavedKnowledgeView, is_default: bool) -> None:
    if is_default:
        SavedKnowledgeView.objects.select_for_update().filter(
            default_slot=1
        ).exclude(pk=view.pk).update(default_slot=None)
        view.default_slot = 1
    else:
        view.default_slot = None


@require_http_methods(["GET", "POST"])
def saved_knowledge_views(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        views = list(SavedKnowledgeView.objects.all())
        return JsonResponse(
            {
                "count": len(views),
                "results": [saved_knowledge_view_payload(view) for view in views],
            }
        )

    try:
        data = parse_json(request)
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"error": "요청 본문은 JSON 객체여야 합니다."}, status=400)
    unknown = set(data) - {"name", "filters", "sort", "is_default"}
    if unknown:
        return JsonResponse(
            {"error": f"지원하지 않는 필드입니다: {', '.join(sorted(unknown))}"},
            status=400,
        )
    if not isinstance(data.get("name"), str):
        return JsonResponse({"error": "name은 문자열이어야 합니다."}, status=400)
    is_default = data.get("is_default", False)
    if type(is_default) is not bool:
        return JsonResponse({"error": "is_default는 boolean이어야 합니다."}, status=400)

    view = SavedKnowledgeView(name=data["name"])
    try:
        update_saved_view_filters(view, data, creating=True)
        view.full_clean(validate_unique=False)
    except KnowledgeFilterError as error:
        return knowledge_filter_error(error)
    except ValidationError as error:
        return saved_view_validation_error(error)

    try:
        with transaction.atomic():
            if SavedKnowledgeView.objects.filter(
                identity_hash=view.identity_hash
            ).exists():
                return JsonResponse(
                    {"error": "같은 이름의 저장된 보기가 있습니다.", "code": "duplicate_name"},
                    status=409,
                )
            view.save()
            if is_default:
                set_saved_view_default(view, True)
                view.save(update_fields=["default_slot", "updated_at"])
    except IntegrityError:
        return JsonResponse(
            {"error": "저장된 보기 충돌이 발생했습니다.", "code": "saved_view_conflict"},
            status=409,
        )
    return JsonResponse(saved_knowledge_view_payload(view), status=201)


@require_http_methods(["GET", "PATCH", "DELETE"])
def saved_knowledge_view_detail(
    request: HttpRequest,
    view_id: int,
) -> JsonResponse | HttpResponse:
    if request.method == "GET":
        view = get_object_or_404(SavedKnowledgeView, pk=view_id)
        return JsonResponse(saved_knowledge_view_payload(view))
    if request.method == "DELETE":
        get_object_or_404(SavedKnowledgeView, pk=view_id).delete()
        return HttpResponse(status=204)

    try:
        data = parse_json(request)
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    if not isinstance(data, dict) or not data:
        return JsonResponse({"error": "변경할 필드를 입력해주세요."}, status=400)
    unknown = set(data) - {"name", "filters", "sort", "is_default"}
    if unknown:
        return JsonResponse(
            {"error": f"지원하지 않는 필드입니다: {', '.join(sorted(unknown))}"},
            status=400,
        )
    if "name" in data and not isinstance(data["name"], str):
        return JsonResponse({"error": "name은 문자열이어야 합니다."}, status=400)
    if "is_default" in data and type(data["is_default"]) is not bool:
        return JsonResponse({"error": "is_default는 boolean이어야 합니다."}, status=400)

    try:
        with transaction.atomic():
            view = get_object_or_404(
                SavedKnowledgeView.objects.select_for_update(),
                pk=view_id,
            )
            if "name" in data:
                view.name = data["name"]
            update_saved_view_filters(view, data)
            view.full_clean(validate_unique=False)
            if SavedKnowledgeView.objects.exclude(pk=view.pk).filter(
                identity_hash=view.identity_hash
            ).exists():
                return JsonResponse(
                    {"error": "같은 이름의 저장된 보기가 있습니다.", "code": "duplicate_name"},
                    status=409,
                )
            if "is_default" in data:
                set_saved_view_default(view, data["is_default"])
            view.save()
    except KnowledgeFilterError as error:
        return knowledge_filter_error(error)
    except ValidationError as error:
        return saved_view_validation_error(error)
    except IntegrityError:
        return JsonResponse(
            {"error": "저장된 보기 충돌이 발생했습니다.", "code": "saved_view_conflict"},
            status=409,
        )
    return JsonResponse(saved_knowledge_view_payload(view))


@require_GET
def saved_knowledge_view_apply(request: HttpRequest, view_id: int) -> JsonResponse:
    view = get_object_or_404(SavedKnowledgeView, pk=view_id)
    try:
        parsed = parse_saved_filter_values(
            view.canonical_filters,
            sort=view.sort,
            stale_category=True,
        )
    except KnowledgeFilterError as error:
        return knowledge_filter_error(error)
    return JsonResponse(
        {
            **saved_knowledge_view_payload(view),
            "canonical_query": parsed.canonical_query,
        }
    )


def related_knowledge_items(
    item: KnowledgeItem,
    *,
    tag_snapshot_id: int | None = None,
) -> list[dict]:
    if not item.category_id:
        return []
    active_category_ids = Category.active_tree_ids()
    if item.category_id not in active_category_ids or Category.objects.filter(
        parent_id=item.category_id,
        pk__in=active_category_ids,
    ).exists():
        return []
    candidates = list(
        knowledge_base_queryset()
        .filter(
            category_id=item.category_id,
            consumption_state__archived_at__isnull=True,
        )
        .exclude(pk=item.pk)
        .order_by("-generated_at", "-id")[:50]
    )
    attach_tag_labels(candidates, snapshot_id=tag_snapshot_id)
    title_tokens = set(re.findall(r"\w{2,}", item.title.casefold()))
    ranked = sorted(
        enumerate(candidates),
        key=lambda pair: (
            -len(title_tokens & set(re.findall(r"\w{2,}", pair[1].title.casefold()))),
            pair[0],
        ),
    )
    return [knowledge_card_payload(candidate) for _, candidate in ranked[:5]]


def navigation_item_payload(item: KnowledgeItem | None) -> dict | None:
    if item is None:
        return None
    return {
        "id": item.id,
        "title": item.title,
        "detail_url": knowledge_detail_url(item),
    }


@require_GET
def knowledge_navigation(request: HttpRequest, item_id: int) -> JsonResponse:
    try:
        parsed = parse_knowledge_filters(request.GET, allow_pagination=True)
    except KnowledgeFilterError as error:
        return knowledge_filter_error(error)
    tag_snapshot_id = active_tag_snapshot_id()
    queryset = apply_knowledge_filters(
        knowledge_base_queryset(),
        parsed,
        tag_snapshot_id=tag_snapshot_id,
    )
    item_ids = list(queryset.values_list("id", flat=True))
    if item_id not in item_ids:
        if not KnowledgeItem.objects.filter(pk=item_id).exists():
            return JsonResponse({"error": "지식 항목을 찾을 수 없습니다."}, status=404)
        return JsonResponse(
            {
                "error": "현재 항목이 목록 조건에서 벗어났습니다.",
                "code": "context_changed",
            },
            status=409,
        )

    position = item_ids.index(item_id)
    neighbor_ids = [
        neighbor_id
        for neighbor_id in (
            item_ids[position - 1] if position else None,
            item_ids[position + 1] if position + 1 < len(item_ids) else None,
        )
        if neighbor_id is not None
    ]
    neighbors = {
        item.pk: item for item in queryset.filter(pk__in=neighbor_ids)
    }
    item = queryset.get(pk=item_id)
    attach_tag_labels([*neighbors.values(), item], snapshot_id=tag_snapshot_id)
    previous_id = item_ids[position - 1] if position else None
    next_id = item_ids[position + 1] if position + 1 < len(item_ids) else None
    return JsonResponse(
        {
            "previous": navigation_item_payload(neighbors.get(previous_id)),
            "next": navigation_item_payload(neighbors.get(next_id)),
            "position": position + 1,
            "total": len(item_ids),
            "canonical_query": parsed.canonical_query,
            "related": related_knowledge_items(item, tag_snapshot_id=tag_snapshot_id),
        }
    )


def bulk_snapshot_membership(
    snapshot: BulkSelectionSnapshot,
    *,
    tag_snapshot_id: int | None,
) -> list[int]:
    if snapshot.canonical_filter:
        try:
            parsed = parse_knowledge_filters(snapshot.canonical_filter)
        except KnowledgeFilterError as error:
            raise KnowledgeActionError(
                409,
                "snapshot_invalid",
                "bulk snapshot의 canonical filter가 유효하지 않습니다.",
            ) from error
        return list(
            apply_knowledge_filters(
                knowledge_base_queryset(),
                parsed,
                tag_snapshot_id=tag_snapshot_id,
            ).values_list(
                "id",
                flat=True,
            )
        )
    return list(
        visible_knowledge_items()
        .filter(pk__in=snapshot.target_ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def bulk_ineligible_count(item_ids: list[int], action: str, parameters: dict) -> int:
    if action != "category":
        return 0
    category_id = parameters["category_id"]
    if category_id not in Category.active_tree_ids():
        raise KnowledgeActionError(
            409,
            "category_changed",
            "카테고리가 비활성화되었거나 삭제되었습니다.",
        )
    eligible_statuses = {
        KnowledgeItem.Status.PENDING,
        KnowledgeItem.Status.NEEDS_REVIEW,
        KnowledgeItem.Status.CLASSIFIED,
    }
    items = KnowledgeItem.objects.filter(pk__in=item_ids).only(
        "status",
        "source_type",
        "answer",
    )
    return sum(
        item.status not in eligible_statuses
        or (
            item.source_type == KnowledgeItem.SourceType.SLACK_QA
            and not item.answer.strip()
        )
        for item in items
    )


@require_http_methods(["POST"])
def knowledge_bulk_preview(request: HttpRequest) -> JsonResponse:
    try:
        data = parse_json(request)
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"error": "요청 본문은 JSON 객체여야 합니다."}, status=400)
    unknown = set(data) - {"ids", "filter", "action", "parameters"}
    if unknown:
        return JsonResponse(
            {"error": f"지원하지 않는 필드입니다: {', '.join(sorted(unknown))}"},
            status=400,
        )
    if ("ids" in data) == ("filter" in data):
        return JsonResponse(
            {"error": "ids 또는 filter 중 하나만 지정해야 합니다.", "code": "invalid_selection"},
            status=400,
        )

    action = data.get("action")
    parameters = data.get("parameters")
    try:
        canonical_parameters = canonical_action_parameters(action, parameters)
        if "ids" in data:
            target_ids = normalize_target_ids(data["ids"])
            visible_ids = list(
                visible_knowledge_items()
                .filter(pk__in=target_ids)
                .order_by("pk")
                .values_list("pk", flat=True)
            )
            if visible_ids != target_ids:
                raise KnowledgeActionError(
                    409,
                    "target_changed",
                    "존재하고 표시 중인 항목만 선택할 수 있습니다.",
                )
            canonical_filter = {}
        else:
            raw_filter = data["filter"]
            if not isinstance(raw_filter, dict):
                return JsonResponse(
                    {"error": "filter는 객체여야 합니다.", "code": "invalid_filter"},
                    status=400,
                )
            parsed = parse_knowledge_filters(raw_filter, allow_pagination=True)
            tag_snapshot_id = active_tag_snapshot_id()
            target_ids = list(
                apply_knowledge_filters(
                    knowledge_base_queryset(),
                    parsed,
                    tag_snapshot_id=tag_snapshot_id,
                ).values_list("id", flat=True)
            )
            canonical_filter = parsed.values or {"archived": "exclude"}
        ineligible = bulk_ineligible_count(target_ids, action, canonical_parameters)
        raw_token, snapshot = create_snapshot(
            target_ids,
            action,
            canonical_parameters,
            canonical_filter,
        )
    except KnowledgeFilterError as error:
        return knowledge_filter_error(error)
    except KnowledgeActionError as error:
        return knowledge_action_error(error)
    return JsonResponse(
        {
            "token": raw_token,
            "count": snapshot.target_count,
            "ineligible": ineligible,
            "expires_at": snapshot.expires_at,
        },
        status=201,
    )


@require_http_methods(["POST"])
def knowledge_bulk_execute(request: HttpRequest) -> JsonResponse:
    try:
        data = parse_json(request)
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"error": "요청 본문은 JSON 객체여야 합니다."}, status=400)
    unknown = set(data) - {"token", "action", "parameters", "review_note"}
    if unknown:
        return JsonResponse(
            {"error": f"지원하지 않는 필드입니다: {', '.join(sorted(unknown))}"},
            status=400,
        )
    if not isinstance(data.get("token"), str) or not data["token"]:
        return JsonResponse(
            {"error": "token은 비어 있지 않은 문자열이어야 합니다.", "code": "invalid_token"},
            status=400,
        )
    try:
        tag_snapshot_id = active_tag_snapshot_id()
        snapshot = execute_snapshot(
            data["token"],
            data.get("action"),
            data.get("parameters"),
            lambda snapshot: bulk_snapshot_membership(
                snapshot,
                tag_snapshot_id=tag_snapshot_id,
            ),
            reviewer=request.user if request.user.is_authenticated else None,
            review_note=data.get("review_note", ""),
        )
    except KnowledgeActionError as error:
        return knowledge_action_error(error)
    payload = {
        "count": len(snapshot.affected_ids),
        "affected_ids": snapshot.affected_ids,
    }
    if snapshot.action_type == "hide":
        payload["undo_expires_at"] = snapshot.consumed_at + UNDO_WINDOW
    return JsonResponse(payload)


@require_http_methods(["POST"])
def knowledge_bulk_undo(request: HttpRequest) -> JsonResponse:
    try:
        data = parse_json(request)
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    if not isinstance(data, dict) or set(data) != {"token"}:
        return JsonResponse(
            {"error": "token만 지정해야 합니다.", "code": "invalid_token"},
            status=400,
        )
    if not isinstance(data["token"], str) or not data["token"]:
        return JsonResponse(
            {"error": "token은 비어 있지 않은 문자열이어야 합니다.", "code": "invalid_token"},
            status=400,
        )
    try:
        affected_ids = undo_hide_snapshot(data["token"])
    except KnowledgeActionError as error:
        return knowledge_action_error(error)
    return JsonResponse({"count": len(affected_ids), "affected_ids": affected_ids})


@require_GET
def knowledge_trash(request: HttpRequest) -> JsonResponse:
    pagination = pagination_params(request)
    if isinstance(pagination, JsonResponse):
        return pagination
    limit, offset = pagination
    queryset = (
        KnowledgeItem.objects.filter(hidden_at__isnull=False)
        .select_related("category", "consumption_state", "content_run", "content_run__job")
        .order_by("-hidden_at", "-id")
    )
    total = queryset.count()
    items = list(queryset[offset : offset + limit])
    attach_tag_labels(items, snapshot_id=active_tag_snapshot_id())
    return JsonResponse(
        {
            "count": total,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
            "results": [
                {**knowledge_card_payload(item), "hidden_at": item.hidden_at}
                for item in items
            ],
        }
    )


@require_http_methods(["POST"])
def knowledge_restore(_request: HttpRequest, item_id: int) -> HttpResponse | JsonResponse:
    try:
        restore_items([item_id])
    except KnowledgeActionError as error:
        return knowledge_action_error(error)
    return HttpResponse(status=204)


@require_http_methods(["GET", "DELETE"])
def knowledge_detail(request: HttpRequest, item_id: int) -> JsonResponse | HttpResponse:
    item = (
        visible_knowledge_items()
        .select_related("category", "consumption_state", "content_run", "reviewed_by")
        .filter(pk=item_id)
        .first()
    )
    if not item:
        return JsonResponse({"error": "지식 항목을 찾을 수 없습니다."}, status=404)
    if request.method == "DELETE":
        hidden_at = timezone.now()
        with transaction.atomic():
            KnowledgeItem.objects.filter(pk=item.pk).update(hidden_at=hidden_at)
            if item.content_run_id:
                ContentRun.objects.filter(pk=item.content_run_id).update(
                    hidden_at=hidden_at
                )
        return HttpResponse(status=204)
    attach_tag_labels([item], snapshot_id=active_tag_snapshot_id())
    return JsonResponse(knowledge_detail_payload(item))


@require_http_methods(["PUT", "PATCH"])
def knowledge_tags(request: HttpRequest, item_id: int) -> JsonResponse:
    if not request.user.is_authenticated:
        return JsonResponse({"error": "인증이 필요합니다.", "code": "auth_required"}, status=403)
    try:
        data = parse_json(request)
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    if not isinstance(data, dict) or set(data) != {"tags"}:
        return JsonResponse({"error": "tags 필드만 지정할 수 있습니다."}, status=400)
    try:
        labels = replace_item_tags(item_id, data["tags"])
    except ValidationError as error:
        return JsonResponse(
            {"error": "; ".join(error.messages), "code": "invalid_tags"},
            status=400,
        )
    return JsonResponse({"id": item_id, "tags": labels})


@require_http_methods(["GET", "PATCH"])
def knowledge_state(request: HttpRequest, item_id: int) -> JsonResponse:
    if request.method == "GET":
        item = (
            visible_knowledge_items()
            .select_related("consumption_state")
            .filter(pk=item_id)
            .first()
        )
        if not item:
            return JsonResponse({"error": "지식 항목을 찾을 수 없습니다."}, status=404)
        return JsonResponse(state_payload(state_for_item(item)))
    try:
        data = parse_json(request)
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    validation_error = validate_consumption_patch(data)
    if validation_error:
        return JsonResponse({"error": validation_error}, status=400)
    return update_consumption_state(item_id, data)


@require_http_methods(["PATCH"])
def knowledge_classification(request: HttpRequest, item_id: int) -> JsonResponse:
    item = get_object_or_404(visible_knowledge_items(), pk=item_id)
    try:
        data = parse_json(request)
        category_id = int(data.get("category_id", ""))
        review_note = str(data.get("review_note", "")).strip()
        reviewer = request.user if request.user.is_authenticated else None
        updated, _ = approve_knowledge_items(
            [item.pk],
            category_id,
            reviewer,
            review_note,
        )
    except (TypeError, ValueError):
        return JsonResponse({"error": "카테고리를 선택해주세요."}, status=400)
    except ValidationError as error:
        return JsonResponse({"error": "; ".join(error.messages)}, status=400)
    if not updated:
        return JsonResponse(
            {"error": "답변이 완료된 분류 대기 또는 검토 항목만 분류할 수 있습니다."},
            status=409,
        )
    item = KnowledgeItem.objects.select_related(
        "category", "consumption_state", "reviewed_by"
    ).get(pk=item.pk)
    attach_tag_labels([item], snapshot_id=active_tag_snapshot_id())
    return JsonResponse(knowledge_detail_payload(item))


@require_GET
def free_question(request: HttpRequest) -> JsonResponse:
    pagination = pagination_params(request)
    if isinstance(pagination, JsonResponse):
        return pagination
    limit, offset = pagination
    queryset = active_knowledge_items().select_related("consumption_state").filter(
        source_type=KnowledgeItem.SourceType.SLACK_QA,
        status__in=(
            KnowledgeItem.Status.AWAITING_ANSWER,
            KnowledgeItem.Status.PENDING,
            KnowledgeItem.Status.NEEDS_REVIEW,
        ),
    )
    query = search_query(request)
    if isinstance(query, JsonResponse):
        return query
    queryset = filter_knowledge_search(queryset, query)
    queryset = sort_knowledge(request, queryset)
    if isinstance(queryset, JsonResponse):
        return queryset
    total = queryset.count()
    items = list(queryset[offset : offset + limit])
    attach_tag_labels(items, snapshot_id=active_tag_snapshot_id())
    return JsonResponse(
        {
            "count": total,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
            "results": [knowledge_card_payload(item) for item in items],
        }
    )


@require_http_methods(["GET", "POST"])
def schedule(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        queryset = ScheduleEvent.objects.select_related("todo_category")
        grouped_value = request.GET.get("grouped", "")
        if grouped_value not in ("", "1"):
            return JsonResponse({"error": "grouped는 1이어야 합니다."}, status=400)
        grouped = grouped_value == "1"
        from_value = request.GET.get("from", "")
        to_value = request.GET.get("to", "")
        from_date = parse_date(from_value) if from_value else None
        to_date = parse_date(to_value) if to_value else None
        if from_value and not from_date or to_value and not to_date:
            return JsonResponse({"error": "날짜는 YYYY-MM-DD 형식이어야 합니다."}, status=400)
        current_timezone = timezone.get_current_timezone()
        if from_date:
            from_start = timezone.make_aware(datetime.combine(from_date, time.min), current_timezone)
            queryset = queryset.filter(
                Q(starts_at__gte=from_start)
                | Q(ends_at__isnull=False, ends_at__gte=from_start)
            )
        if to_date:
            to_end = timezone.make_aware(
                datetime.combine(to_date + timedelta(days=1), time.min),
                current_timezone,
            )
            queryset = queryset.filter(starts_at__lt=to_end)
        total = queryset.count()
        if grouped:
            events = list(queryset)
            return JsonResponse(
                {
                    "count": total,
                    "results": [
                        schedule_payload(
                            event,
                            agenda_group_value=agenda_group(event).value,
                        )
                        for event in events
                    ],
                    "group_counts": agenda_group_counts(events),
                }
            )
        events = list(queryset[:200])
        return JsonResponse(
            {"count": total, "results": [schedule_payload(event) for event in events]}
        )

    try:
        data = parse_json(request)
        event = ScheduleEvent()
        update_schedule_event(event, data)
        event.save()
    except (ValueError, ValidationError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(schedule_payload(event), status=201)


@require_http_methods(["PATCH", "DELETE"])
def schedule_detail(request: HttpRequest, event_id: int) -> JsonResponse | HttpResponse:
    event = get_object_or_404(
        ScheduleEvent.objects.select_related("todo_category"),
        pk=event_id,
    )
    if request.method == "DELETE":
        if event.source_type == ScheduleEvent.SourceType.SLACK:
            return JsonResponse(
                {"error": "Slack에서 등록된 일정은 Slack 메시지를 삭제해주세요."},
                status=409,
            )
        event.delete()
        return HttpResponse(status=204)
    try:
        data = parse_json(request)
        if (
            event.source_type == ScheduleEvent.SourceType.SLACK
            and set(data) - {"completed", "todo_category_id"}
        ):
            return JsonResponse(
                {"error": "Slack 일정의 내용은 Slack 메시지에서 수정해주세요."},
                status=409,
            )
        update_schedule_event(event, data)
        event.save()
    except (ValueError, ValidationError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(schedule_payload(event))


@require_http_methods(["GET", "POST"])
def schedule_categories(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        categories = ScheduleCategory.objects.annotate(usage_count=Count("events"))
        return JsonResponse(
            {"results": [schedule_category_payload(category) for category in categories]}
        )
    try:
        data = parse_json(request)
        category = ScheduleCategory(sort_order=0)
        update_schedule_category(category, data)
        category.save()
        reclassify_automatic_todos()
    except (ValueError, ValidationError) as error:
        return JsonResponse({"error": str(error)}, status=400)
    return JsonResponse(schedule_category_payload(category), status=201)


@require_http_methods(["PATCH", "DELETE"])
def schedule_category_detail(
    request: HttpRequest,
    category_id: int,
) -> JsonResponse | HttpResponse:
    category = get_object_or_404(ScheduleCategory, pk=category_id)
    if request.method == "DELETE":
        if category.is_fallback:
            return JsonResponse({"error": "기본 카테고리는 삭제할 수 없습니다."}, status=409)
        try:
            category.delete()
        except ProtectedError:
            return JsonResponse(
                {"error": "사용 중인 카테고리는 삭제할 수 없습니다."},
                status=409,
            )
        return HttpResponse(status=204)
    try:
        data = parse_json(request)
        update_schedule_category(category, data)
        category.save()
        reclassify_automatic_todos()
    except (ValueError, ValidationError) as error:
        return JsonResponse({"error": str(error)}, status=400)
    return JsonResponse(schedule_category_payload(category))


@require_GET
def runs(request: HttpRequest) -> JsonResponse:
    current_session = session_key(request)
    queryset = run_queryset(current_session)
    category = request.GET.get("category", "")
    status = request.GET.get("status", "")
    bookmarked = request.GET.get("bookmarked") == "1"
    if category:
        queryset = queryset.filter(job__category=category)
    if status:
        queryset = queryset.filter(status=status)
    if bookmarked:
        queryset = queryset.filter(
            knowledge_item__consumption_state__bookmarked_at__isnull=False,
        )
    try:
        limit = min(max(int(request.GET.get("limit", "50")), 1), 100)
    except ValueError:
        limit = 50
    results = list(queryset[:limit])
    tag_snapshot_id = active_tag_snapshot_id()
    return JsonResponse(
        {
            "results": [
                run_payload(run, current_session, tag_snapshot_id=tag_snapshot_id)
                for run in results
            ]
        }
    )


@require_http_methods(["GET", "DELETE"])
def run_detail(request: HttpRequest, run_id: int) -> JsonResponse | HttpResponse:
    current_session = session_key(request)
    run = get_object_or_404(
        run_queryset(current_session, include_archived=True), pk=run_id
    )
    if request.method == "DELETE":
        hidden_at = timezone.now()
        with transaction.atomic():
            ContentRun.objects.filter(pk=run.pk).update(hidden_at=hidden_at)
            KnowledgeItem.objects.filter(content_run=run).update(hidden_at=hidden_at)
        return HttpResponse(status=204)
    return JsonResponse(
        run_payload(
            run,
            current_session,
            detail=True,
            tag_snapshot_id=active_tag_snapshot_id(),
        )
    )


@require_http_methods(["POST", "PATCH"])
def run_state(request: HttpRequest, run_id: int) -> JsonResponse:
    item = (
        visible_knowledge_items()
        .filter(content_run_id=run_id, content_run__hidden_at__isnull=True)
        .first()
    )
    if not item:
        return JsonResponse({"error": "지식 항목을 찾을 수 없습니다."}, status=404)
    try:
        data = parse_json(request)
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    validation_error = validate_consumption_patch(data)
    if validation_error:
        return JsonResponse({"error": validation_error}, status=400)
    return update_consumption_state(item.pk, data)


@require_http_methods(["POST"])
def run_responses(request: HttpRequest, run_id: int) -> JsonResponse:
    run = get_object_or_404(ContentRun, pk=run_id, hidden_at__isnull=True)
    try:
        data = parse_json(request)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    answer = str(data.get("answer", "")).strip()
    if not answer:
        return JsonResponse({"error": "답변을 입력해주세요."}, status=400)
    response = UserResponse.objects.create(
        run=run,
        session_key=session_key(request, create=True),
        question_key=str(data.get("question_key", ""))[:200],
        answer=answer[:10000],
    )
    return JsonResponse(
        {
            "id": response.id,
            "question_key": response.question_key,
            "answer": response.answer,
            "feedback": response.feedback,
            "score": response.score,
            "created_at": response.created_at,
        },
        status=201,
    )
