import hashlib
import json
from functools import wraps
from uuid import UUID

from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Max, Q
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from .models import (
    KnowledgeItem,
    PlatformAgent,
    PlatformApiToken,
    PlatformApproval,
    PlatformArtifact,
    PlatformEvent,
    PlatformIdempotencyRecord,
    PlatformInboxItem,
    PlatformTask,
)
from .platform_storage import ArtifactStorageError, read_artifact, write_artifact


class PlatformApiError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details


def error_response(error: PlatformApiError) -> JsonResponse:
    payload = {"code": error.code, "message": error.message}
    if error.details is not None:
        payload["details"] = error.details
    return JsonResponse({"error": payload}, status=error.status)


def platform_auth(scopes_by_method: dict[str, str]):
    def decorator(view):
        @wraps(view)
        def wrapped(request: HttpRequest, *args, **kwargs):
            authorization = request.headers.get("Authorization", "")
            scheme, _, raw_token = authorization.partition(" ")
            if scheme.casefold() != "bearer" or not raw_token:
                return error_response(
                    PlatformApiError(
                        "authentication_required",
                        "Bearer 인증 토큰이 필요합니다.",
                        status=401,
                    )
                )
            token = PlatformApiToken.authenticate(raw_token.strip())
            if not token:
                return error_response(
                    PlatformApiError(
                        "invalid_token",
                        "유효하지 않거나 만료된 인증 토큰입니다.",
                        status=401,
                    )
                )
            required_scope = scopes_by_method.get(request.method)
            if required_scope and not token.allows(required_scope):
                return error_response(
                    PlatformApiError(
                        "insufficient_scope",
                        f"필요한 권한 범위가 없습니다: {required_scope}",
                        status=403,
                    )
                )
            request.platform_token = token
            try:
                return view(request, *args, **kwargs)
            except PlatformApiError as error:
                return error_response(error)

        return wrapped

    return decorator


class Idempotency:
    def __init__(self, request: HttpRequest, token: PlatformApiToken, key: str, request_hash: str):
        self.request = request
        self.token = token
        self.key = key
        self.request_hash = request_hash

    @classmethod
    def from_request(cls, request: HttpRequest, token: PlatformApiToken):
        key = request.headers.get("Idempotency-Key", "").strip()
        if not key:
            raise PlatformApiError(
                "idempotency_key_required",
                "변경 요청에는 Idempotency-Key 헤더가 필요합니다.",
                status=428,
            )
        if len(key) > 128:
            raise PlatformApiError(
                "invalid_idempotency_key",
                "Idempotency-Key는 128자 이하여야 합니다.",
            )
        return cls(request, token, key, hashlib.sha256(request.body).hexdigest())

    def existing_response(self) -> JsonResponse | None:
        record = PlatformIdempotencyRecord.objects.filter(
            token=self.token,
            key=self.key,
            method=self.request.method,
            path=self.request.path,
        ).first()
        if not record:
            return None
        if record.request_hash != self.request_hash:
            raise PlatformApiError(
                "idempotency_conflict",
                "같은 Idempotency-Key가 다른 요청 본문에 사용되었습니다.",
                status=409,
            )
        response = JsonResponse(record.response_body, status=record.response_status)
        response["Idempotency-Replayed"] = "true"
        return response

    def store(self, payload: dict, status: int) -> None:
        PlatformIdempotencyRecord.objects.create(
            token=self.token,
            key=self.key,
            method=self.request.method,
            path=self.request.path,
            request_hash=self.request_hash,
            response_status=status,
            response_body=json.loads(json.dumps(payload, cls=DjangoJSONEncoder)),
        )


def idempotent_mutation(request: HttpRequest) -> tuple[Idempotency, JsonResponse | None]:
    idempotency = Idempotency.from_request(request, request.platform_token)
    return idempotency, idempotency.existing_response()


def parse_json_object(request: HttpRequest) -> dict:
    try:
        value = json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        raise PlatformApiError("invalid_json", "올바른 JSON 요청이 아닙니다.") from exc
    if not isinstance(value, dict):
        raise PlatformApiError("invalid_request", "요청 본문은 JSON 객체여야 합니다.")
    return value


def reject_unknown(data: dict, allowed: set[str]) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise PlatformApiError(
            "unknown_fields",
            "지원하지 않는 필드가 포함되어 있습니다.",
            details=unknown,
        )


def string_value(
    data: dict,
    key: str,
    *,
    required: bool = False,
    max_length: int,
    allow_blank: bool = True,
) -> str:
    if key not in data:
        if required:
            raise PlatformApiError("missing_field", f"필수 필드가 없습니다: {key}")
        return ""
    value = data[key]
    if not isinstance(value, str):
        raise PlatformApiError("invalid_field", f"{key}는 문자열이어야 합니다.")
    value = value.strip()
    if not allow_blank and not value:
        raise PlatformApiError("invalid_field", f"{key}는 비어 있을 수 없습니다.")
    if len(value) > max_length:
        raise PlatformApiError("invalid_field", f"{key}는 {max_length}자 이하여야 합니다.")
    return value


def text_value(
    data: dict,
    key: str,
    *,
    required: bool = False,
    max_length: int,
) -> str:
    if key not in data:
        if required:
            raise PlatformApiError("missing_field", f"필수 필드가 없습니다: {key}")
        return ""
    value = data[key]
    if not isinstance(value, str):
        raise PlatformApiError("invalid_field", f"{key}는 문자열이어야 합니다.")
    if required and not value.strip():
        raise PlatformApiError("invalid_field", f"{key}는 비어 있을 수 없습니다.")
    if len(value) > max_length:
        raise PlatformApiError("invalid_field", f"{key}는 {max_length}자 이하여야 합니다.")
    return value


def dictionary_value(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise PlatformApiError("invalid_field", f"{key}는 JSON 객체여야 합니다.")
    return value


def string_list_value(data: dict, key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PlatformApiError("invalid_field", f"{key}는 문자열 배열이어야 합니다.")
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


def uuid_value(value, field: str) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise PlatformApiError("invalid_field", f"{field}는 올바른 UUID여야 합니다.") from exc


def validate_url(value: str, field: str) -> None:
    if not value:
        return
    try:
        URLValidator(schemes=["http", "https"])(value)
    except Exception as exc:
        raise PlatformApiError(
            "invalid_field",
            f"{field}는 올바른 HTTP(S) URL이어야 합니다.",
        ) from exc


def iso(value) -> str | None:
    return value.isoformat() if value else None


def pagination(request: HttpRequest) -> tuple[int, int]:
    try:
        limit = int(request.GET.get("limit", "50"))
        offset = int(request.GET.get("offset", "0"))
    except ValueError as exc:
        raise PlatformApiError("invalid_pagination", "limit과 offset은 정수여야 합니다.") from exc
    if not 1 <= limit <= 100 or offset < 0:
        raise PlatformApiError(
            "invalid_pagination",
            "limit은 1~100, offset은 0 이상이어야 합니다.",
        )
    return limit, offset


def list_payload(request: HttpRequest, queryset, serializer) -> dict:
    limit, offset = pagination(request)
    count = queryset.count()
    results = [serializer(item) for item in queryset[offset : offset + limit]]
    next_offset = offset + limit if offset + limit < count else None
    return {
        "data": results,
        "pagination": {
            "count": count,
            "limit": limit,
            "offset": offset,
            "next_offset": next_offset,
        },
    }


def agent_payload(agent: PlatformAgent) -> dict:
    return {
        "key": agent.key,
        "name": agent.name,
        "capabilities": agent.capabilities,
        "metadata": agent.metadata,
        "is_active": agent.is_active,
    }


def inbox_payload(item: PlatformInboxItem) -> dict:
    return {
        "id": str(item.id),
        "source_type": item.source_type,
        "external_id": item.external_id or "",
        "title": item.title,
        "content": item.content,
        "source_url": item.source_url,
        "payload": item.payload,
        "status": item.status,
        "status_label": item.get_status_display(),
        "collected_by": item.collected_by.key,
        "collected_at": iso(item.collected_at),
        "created_at": iso(item.created_at),
        "updated_at": iso(item.updated_at),
    }


def task_payload(task: PlatformTask) -> dict:
    return {
        "id": str(task.id),
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "status_label": task.get_status_display(),
        "priority": task.priority,
        "priority_label": task.get_priority_display(),
        "inbox_item_id": str(task.inbox_item_id) if task.inbox_item_id else None,
        "created_by": task.created_by.key,
        "assigned_agents": task.assigned_agents,
        "metadata": task.metadata,
        "due_at": iso(task.due_at),
        "completed_at": iso(task.completed_at),
        "created_at": iso(task.created_at),
        "updated_at": iso(task.updated_at),
    }


def artifact_payload(artifact: PlatformArtifact, *, include_content: bool = False) -> dict:
    payload = {
        "id": str(artifact.id),
        "series_id": str(artifact.series_id),
        "version": artifact.version,
        "task_id": str(artifact.task_id),
        "kind": artifact.kind,
        "kind_label": artifact.get_kind_display(),
        "title": artifact.title,
        "mime_type": artifact.mime_type,
        "storage_uri": f"artifact://{artifact.series_id}/{artifact.version}",
        "sha256": artifact.content_sha256,
        "size_bytes": artifact.size_bytes,
        "created_by": artifact.created_by.key,
        "metadata": artifact.metadata,
        "created_at": iso(artifact.created_at),
    }
    if include_content:
        try:
            payload["content"] = read_artifact(
                artifact.artifact_path,
                artifact.content_sha256,
            )
        except ArtifactStorageError as exc:
            raise PlatformApiError(
                "artifact_unavailable",
                str(exc),
                status=409,
            ) from exc
    return payload


def approval_payload(approval: PlatformApproval) -> dict:
    return {
        "id": str(approval.id),
        "task_id": str(approval.task_id),
        "artifact_id": str(approval.artifact_id),
        "target_sha256": approval.target_sha256,
        "status": approval.status,
        "status_label": approval.get_status_display(),
        "request_note": approval.request_note,
        "decision_note": approval.decision_note,
        "requested_by": approval.requested_by.key,
        "decided_by": approval.decided_by.key if approval.decided_by_id else None,
        "requested_at": iso(approval.requested_at),
        "decided_at": iso(approval.decided_at),
    }


def event_payload(event: PlatformEvent) -> dict:
    return {
        "id": str(event.id),
        "event_type": event.event_type,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "task_id": str(event.task_id) if event.task_id else None,
        "actor": event.actor.key,
        "data": event.data,
        "created_at": iso(event.created_at),
    }


def record_event(
    *,
    event_type: str,
    entity_type: str,
    entity_id,
    actor: PlatformAgent,
    task: PlatformTask | None = None,
    data: dict | None = None,
) -> PlatformEvent:
    return PlatformEvent.objects.create(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=str(entity_id),
        actor=actor,
        task=task,
        data=data or {},
    )


def validate_agent_keys(agent_keys: list[str]) -> None:
    existing = set(
        PlatformAgent.objects.filter(key__in=agent_keys, is_active=True).values_list(
            "key", flat=True
        )
    )
    missing = sorted(set(agent_keys) - existing)
    if missing:
        raise PlatformApiError(
            "unknown_agents",
            "등록되지 않았거나 비활성화된 에이전트입니다.",
            details=missing,
        )


TASK_TRANSITIONS = {
    PlatformTask.Status.COLLECTED: {
        PlatformTask.Status.ANALYZING,
        PlatformTask.Status.DRAFT,
        PlatformTask.Status.NEEDS_REVIEW,
        PlatformTask.Status.FAILED,
    },
    PlatformTask.Status.ANALYZING: {
        PlatformTask.Status.DRAFT,
        PlatformTask.Status.NEEDS_REVIEW,
        PlatformTask.Status.FAILED,
    },
    PlatformTask.Status.DRAFT: {
        PlatformTask.Status.NEEDS_REVIEW,
        PlatformTask.Status.REVISION_REQUESTED,
        PlatformTask.Status.FAILED,
    },
    PlatformTask.Status.NEEDS_REVIEW: {
        PlatformTask.Status.APPROVED,
        PlatformTask.Status.REJECTED,
        PlatformTask.Status.REVISION_REQUESTED,
        PlatformTask.Status.FAILED,
    },
    PlatformTask.Status.APPROVED: {
        PlatformTask.Status.QUEUED,
        PlatformTask.Status.COMPLETED,
        PlatformTask.Status.REVISION_REQUESTED,
    },
    PlatformTask.Status.REJECTED: {PlatformTask.Status.REVISION_REQUESTED},
    PlatformTask.Status.REVISION_REQUESTED: {
        PlatformTask.Status.ANALYZING,
        PlatformTask.Status.DRAFT,
        PlatformTask.Status.NEEDS_REVIEW,
        PlatformTask.Status.FAILED,
    },
    PlatformTask.Status.QUEUED: {
        PlatformTask.Status.EXECUTING,
        PlatformTask.Status.FAILED,
    },
    PlatformTask.Status.EXECUTING: {
        PlatformTask.Status.COMPLETED,
        PlatformTask.Status.FAILED,
    },
    PlatformTask.Status.FAILED: {
        PlatformTask.Status.QUEUED,
        PlatformTask.Status.ANALYZING,
    },
    PlatformTask.Status.COMPLETED: set(),
}


def transition_task(task: PlatformTask, status: str, actor: PlatformAgent) -> None:
    if status == task.status:
        return
    if status not in TASK_TRANSITIONS.get(task.status, set()):
        raise PlatformApiError(
            "invalid_transition",
            f"허용되지 않는 상태 전이입니다: {task.status} → {status}",
            status=409,
        )
    previous = task.status
    task.status = status
    task.completed_at = timezone.now() if status == PlatformTask.Status.COMPLETED else None
    task.save(update_fields=["status", "completed_at", "updated_at"])
    record_event(
        event_type="task.status_changed",
        entity_type="task",
        entity_id=task.id,
        task=task,
        actor=actor,
        data={"from": previous, "to": status},
    )


@csrf_exempt
@require_GET
@platform_auth({"GET": "platform:read"})
def platform_root(request: HttpRequest) -> JsonResponse:
    return JsonResponse(
        {
            "data": {
                "name": "Dashboard Platform API",
                "version": "v1",
                "authenticated_agent": agent_payload(request.platform_token.agent),
                "capabilities": [
                    "inbox",
                    "tasks",
                    "artifacts",
                    "approvals",
                    "events",
                    "agents",
                    "search",
                ],
            }
        }
    )


@csrf_exempt
@require_GET
@platform_auth({"GET": "platform:read"})
def agents(request: HttpRequest) -> JsonResponse:
    queryset = PlatformAgent.objects.filter(is_active=True)
    return JsonResponse(list_payload(request, queryset, agent_payload))


@csrf_exempt
@require_http_methods(["GET", "POST"])
@platform_auth({"GET": "platform:read", "POST": "inbox:write"})
def inbox_collection(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        queryset = PlatformInboxItem.objects.select_related("collected_by")
        if status := request.GET.get("status"):
            queryset = queryset.filter(status=status)
        if source_type := request.GET.get("source_type"):
            queryset = queryset.filter(source_type=source_type)
        return JsonResponse(list_payload(request, queryset, inbox_payload))

    idempotency, replay = idempotent_mutation(request)
    if replay:
        return replay
    data = parse_json_object(request)
    reject_unknown(data, {"source_type", "external_id", "title", "content", "source_url", "payload"})
    source_type = string_value(data, "source_type", required=True, max_length=60, allow_blank=False)
    external_id = string_value(data, "external_id", max_length=200)
    title = string_value(data, "title", required=True, max_length=250, allow_blank=False)
    content = text_value(data, "content", max_length=2_000_000)
    source_url = string_value(data, "source_url", max_length=1000)
    validate_url(source_url, "source_url")
    item_payload = dictionary_value(data, "payload")

    with transaction.atomic():
        PlatformApiToken.objects.select_for_update().get(pk=request.platform_token.pk)
        replay = idempotency.existing_response()
        if replay:
            return replay
        if external_id and PlatformInboxItem.objects.filter(
            source_type=source_type,
            external_id=external_id or None,
        ).exists():
            raise PlatformApiError(
                "source_conflict",
                "같은 source_type과 external_id의 수집 항목이 이미 존재합니다.",
                status=409,
            )
        item = PlatformInboxItem.objects.create(
            source_type=source_type,
            external_id=external_id,
            title=title,
            content=content,
            source_url=source_url,
            payload=item_payload,
            collected_by=request.platform_token.agent,
        )
        record_event(
            event_type="inbox.collected",
            entity_type="inbox",
            entity_id=item.id,
            actor=request.platform_token.agent,
            data={"source_type": item.source_type, "external_id": item.external_id},
        )
        payload = {"data": inbox_payload(item)}
        idempotency.store(payload, 201)
    return JsonResponse(payload, status=201)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@platform_auth({"GET": "platform:read", "POST": "tasks:write"})
def task_collection(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        queryset = PlatformTask.objects.select_related("created_by", "inbox_item")
        if status := request.GET.get("status"):
            queryset = queryset.filter(status=status)
        if assigned_agent := request.GET.get("assigned_agent"):
            queryset = queryset.filter(assigned_agents__contains=[assigned_agent])
        return JsonResponse(list_payload(request, queryset, task_payload))

    idempotency, replay = idempotent_mutation(request)
    if replay:
        return replay
    data = parse_json_object(request)
    reject_unknown(
        data,
        {"title", "description", "priority", "inbox_item_id", "assigned_agents", "metadata", "due_at"},
    )
    title = string_value(data, "title", required=True, max_length=250, allow_blank=False)
    description = text_value(data, "description", max_length=2_000_000)
    priority = string_value(data, "priority", max_length=10) or PlatformTask.Priority.NORMAL
    if priority not in PlatformTask.Priority.values:
        raise PlatformApiError("invalid_field", "지원하지 않는 priority 값입니다.")
    assigned_agents = string_list_value(data, "assigned_agents")
    validate_agent_keys(assigned_agents)
    metadata = dictionary_value(data, "metadata")
    inbox_item = None
    if data.get("inbox_item_id"):
        inbox_item = PlatformInboxItem.objects.filter(
            pk=uuid_value(data["inbox_item_id"], "inbox_item_id")
        ).first()
        if not inbox_item:
            raise PlatformApiError("not_found", "수집 항목을 찾을 수 없습니다.", status=404)
    due_at = None
    if data.get("due_at"):
        due_at = parse_datetime(str(data["due_at"]))
        if not due_at:
            raise PlatformApiError("invalid_field", "due_at은 ISO 8601 날짜·시간이어야 합니다.")

    with transaction.atomic():
        PlatformApiToken.objects.select_for_update().get(pk=request.platform_token.pk)
        replay = idempotency.existing_response()
        if replay:
            return replay
        task = PlatformTask.objects.create(
            title=title,
            description=description,
            priority=priority,
            inbox_item=inbox_item,
            created_by=request.platform_token.agent,
            assigned_agents=assigned_agents,
            metadata=metadata,
            due_at=due_at,
        )
        record_event(
            event_type="task.created",
            entity_type="task",
            entity_id=task.id,
            task=task,
            actor=request.platform_token.agent,
            data={"status": task.status, "assigned_agents": task.assigned_agents},
        )
        payload = {"data": task_payload(task)}
        idempotency.store(payload, 201)
    return JsonResponse(payload, status=201)


@csrf_exempt
@require_http_methods(["GET", "PATCH"])
@platform_auth({"GET": "platform:read", "PATCH": "tasks:write"})
def task_detail(request: HttpRequest, task_id: UUID) -> JsonResponse:
    task = PlatformTask.objects.select_related("created_by", "inbox_item").filter(pk=task_id).first()
    if not task:
        raise PlatformApiError("not_found", "작업을 찾을 수 없습니다.", status=404)
    if request.method == "GET":
        return JsonResponse({"data": task_payload(task)})

    data = parse_json_object(request)
    reject_unknown(data, {"title", "description", "priority", "assigned_agents", "metadata", "due_at", "status"})
    with transaction.atomic():
        task = PlatformTask.objects.select_for_update().get(pk=task_id)
        changed_fields = []
        change_data = {}
        if "title" in data:
            task.title = string_value(data, "title", required=True, max_length=250, allow_blank=False)
            changed_fields.append("title")
        if "description" in data:
            task.description = text_value(data, "description", max_length=2_000_000)
            changed_fields.append("description")
        if "priority" in data:
            priority = string_value(data, "priority", required=True, max_length=10, allow_blank=False)
            if priority not in PlatformTask.Priority.values:
                raise PlatformApiError("invalid_field", "지원하지 않는 priority 값입니다.")
            task.priority = priority
            changed_fields.append("priority")
        if "assigned_agents" in data:
            task.assigned_agents = string_list_value(data, "assigned_agents")
            validate_agent_keys(task.assigned_agents)
            changed_fields.append("assigned_agents")
        if "metadata" in data:
            task.metadata = dictionary_value(data, "metadata")
            changed_fields.append("metadata")
        if "due_at" in data:
            if data["due_at"] is None:
                task.due_at = None
            else:
                task.due_at = parse_datetime(str(data["due_at"]))
                if not task.due_at:
                    raise PlatformApiError("invalid_field", "due_at은 ISO 8601 날짜·시간이어야 합니다.")
            changed_fields.append("due_at")
        if changed_fields:
            task.save(update_fields=[*changed_fields, "updated_at"])
            change_data["fields"] = changed_fields
            record_event(
                event_type="task.updated",
                entity_type="task",
                entity_id=task.id,
                task=task,
                actor=request.platform_token.agent,
                data=change_data,
            )
        if "status" in data:
            status = string_value(data, "status", required=True, max_length=24, allow_blank=False)
            if status not in PlatformTask.Status.values:
                raise PlatformApiError("invalid_field", "지원하지 않는 status 값입니다.")
            transition_task(task, status, request.platform_token.agent)
    return JsonResponse({"data": task_payload(task)})


def task_context_payload(task: PlatformTask) -> dict:
    artifacts = list(task.artifacts.select_related("created_by").order_by("created_at", "version"))
    approvals = list(
        task.approvals.select_related("artifact", "requested_by", "decided_by").order_by("requested_at")
    )
    events = list(task.events.select_related("actor").order_by("created_at", "id"))
    return {
        "task": task_payload(task),
        "inbox_item": inbox_payload(task.inbox_item) if task.inbox_item_id else None,
        "artifacts": [artifact_payload(item, include_content=True) for item in artifacts],
        "approvals": [approval_payload(item) for item in approvals],
        "events": [event_payload(item) for item in events],
        "allowed_status_transitions": sorted(TASK_TRANSITIONS.get(task.status, set())),
    }


@csrf_exempt
@require_GET
@platform_auth({"GET": "platform:read"})
def task_context(request: HttpRequest, task_id: UUID) -> JsonResponse:
    task = (
        PlatformTask.objects.select_related("created_by", "inbox_item", "inbox_item__collected_by")
        .filter(pk=task_id)
        .first()
    )
    if not task:
        raise PlatformApiError("not_found", "작업을 찾을 수 없습니다.", status=404)
    return JsonResponse({"data": task_context_payload(task)})


@csrf_exempt
@require_http_methods(["GET", "POST"])
@platform_auth({"GET": "platform:read", "POST": "artifacts:write"})
def artifact_collection(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        queryset = PlatformArtifact.objects.select_related("created_by", "task")
        if task_id := request.GET.get("task_id"):
            queryset = queryset.filter(task_id=uuid_value(task_id, "task_id"))
        if series_id := request.GET.get("series_id"):
            queryset = queryset.filter(series_id=uuid_value(series_id, "series_id"))
        if kind := request.GET.get("kind"):
            queryset = queryset.filter(kind=kind)
        return JsonResponse(
            list_payload(request, queryset, lambda item: artifact_payload(item, include_content=False))
        )

    idempotency, replay = idempotent_mutation(request)
    if replay:
        return replay
    data = parse_json_object(request)
    reject_unknown(data, {"task_id", "series_id", "kind", "title", "content", "mime_type", "metadata"})
    task_id = uuid_value(data.get("task_id"), "task_id")
    kind = string_value(data, "kind", required=True, max_length=20, allow_blank=False)
    if kind not in PlatformArtifact.Kind.values:
        raise PlatformApiError("invalid_field", "지원하지 않는 kind 값입니다.")
    title = string_value(data, "title", required=True, max_length=250, allow_blank=False)
    content = text_value(data, "content", required=True, max_length=2_000_000)
    mime_type = string_value(data, "mime_type", max_length=100) or "text/markdown"
    metadata = dictionary_value(data, "metadata")
    requested_series = uuid_value(data["series_id"], "series_id") if data.get("series_id") else None

    with transaction.atomic():
        PlatformApiToken.objects.select_for_update().get(pk=request.platform_token.pk)
        replay = idempotency.existing_response()
        if replay:
            return replay
        task = PlatformTask.objects.select_for_update().filter(pk=task_id).first()
        if not task:
            raise PlatformApiError("not_found", "작업을 찾을 수 없습니다.", status=404)
        series_id = requested_series or PlatformArtifact._meta.get_field("series_id").default()
        latest = PlatformArtifact.objects.filter(series_id=series_id).aggregate(version=Max("version"))["version"]
        if latest:
            existing_task_id = PlatformArtifact.objects.filter(series_id=series_id).values_list(
                "task_id", flat=True
            ).first()
            if existing_task_id != task.id:
                raise PlatformApiError(
                    "artifact_series_conflict",
                    "series_id는 다른 작업에서 재사용할 수 없습니다.",
                    status=409,
                )
        version = (latest or 0) + 1
        try:
            path, digest, size_bytes = write_artifact(series_id, version, content, mime_type)
        except ArtifactStorageError as exc:
            raise PlatformApiError("artifact_write_failed", str(exc), status=409) from exc
        artifact = PlatformArtifact.objects.create(
            series_id=series_id,
            version=version,
            task=task,
            kind=kind,
            title=title,
            mime_type=mime_type,
            artifact_path=str(path),
            content_sha256=digest,
            size_bytes=size_bytes,
            created_by=request.platform_token.agent,
            metadata=metadata,
        )
        record_event(
            event_type="artifact.created",
            entity_type="artifact",
            entity_id=artifact.id,
            task=task,
            actor=request.platform_token.agent,
            data={
                "series_id": str(artifact.series_id),
                "version": artifact.version,
                "kind": artifact.kind,
                "sha256": artifact.content_sha256,
            },
        )
        payload = {"data": artifact_payload(artifact, include_content=True)}
        idempotency.store(payload, 201)
    return JsonResponse(payload, status=201)


@csrf_exempt
@require_GET
@platform_auth({"GET": "platform:read"})
def artifact_detail(request: HttpRequest, artifact_id: UUID) -> JsonResponse:
    artifact = (
        PlatformArtifact.objects.select_related("created_by", "task")
        .filter(pk=artifact_id)
        .first()
    )
    if not artifact:
        raise PlatformApiError("not_found", "아티팩트를 찾을 수 없습니다.", status=404)
    return JsonResponse({"data": artifact_payload(artifact, include_content=True)})


@csrf_exempt
@require_http_methods(["GET", "POST"])
@platform_auth({"GET": "platform:read", "POST": "approvals:request"})
def approval_collection(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        queryset = PlatformApproval.objects.select_related(
            "task", "artifact", "requested_by", "decided_by"
        )
        if task_id := request.GET.get("task_id"):
            queryset = queryset.filter(task_id=uuid_value(task_id, "task_id"))
        if status := request.GET.get("status"):
            queryset = queryset.filter(status=status)
        return JsonResponse(list_payload(request, queryset, approval_payload))

    idempotency, replay = idempotent_mutation(request)
    if replay:
        return replay
    data = parse_json_object(request)
    reject_unknown(data, {"task_id", "artifact_id", "note"})
    task_id = uuid_value(data.get("task_id"), "task_id")
    artifact_id = uuid_value(data.get("artifact_id"), "artifact_id")
    note = text_value(data, "note", max_length=10_000)

    with transaction.atomic():
        PlatformApiToken.objects.select_for_update().get(pk=request.platform_token.pk)
        replay = idempotency.existing_response()
        if replay:
            return replay
        task = PlatformTask.objects.select_for_update().filter(pk=task_id).first()
        artifact = PlatformArtifact.objects.filter(pk=artifact_id, task_id=task_id).first()
        if not task or not artifact:
            raise PlatformApiError(
                "not_found",
                "작업 또는 해당 작업의 아티팩트를 찾을 수 없습니다.",
                status=404,
            )
        if PlatformApproval.objects.filter(artifact=artifact).exists():
            raise PlatformApiError(
                "approval_already_requested",
                "이 아티팩트 버전에는 이미 승인 요청이 존재합니다.",
                status=409,
            )
        approval = PlatformApproval.objects.create(
            task=task,
            artifact=artifact,
            target_sha256=artifact.content_sha256,
            request_note=note,
            requested_by=request.platform_token.agent,
        )
        if task.status != PlatformTask.Status.NEEDS_REVIEW:
            transition_task(task, PlatformTask.Status.NEEDS_REVIEW, request.platform_token.agent)
        record_event(
            event_type="approval.requested",
            entity_type="approval",
            entity_id=approval.id,
            task=task,
            actor=request.platform_token.agent,
            data={"artifact_id": str(artifact.id), "target_sha256": artifact.content_sha256},
        )
        payload = {"data": approval_payload(approval)}
        idempotency.store(payload, 201)
    return JsonResponse(payload, status=201)


@csrf_exempt
@require_http_methods(["POST"])
@platform_auth({"POST": "approvals:decide"})
def approval_decision(request: HttpRequest, approval_id: UUID) -> JsonResponse:
    idempotency, replay = idempotent_mutation(request)
    if replay:
        return replay
    data = parse_json_object(request)
    reject_unknown(data, {"decision", "note"})
    decision = string_value(data, "decision", required=True, max_length=24, allow_blank=False)
    allowed = {
        PlatformApproval.Status.APPROVED,
        PlatformApproval.Status.REJECTED,
        PlatformApproval.Status.REVISION_REQUESTED,
    }
    if decision not in allowed:
        raise PlatformApiError("invalid_field", "지원하지 않는 승인 결정입니다.")
    note = text_value(data, "note", max_length=10_000)

    with transaction.atomic():
        PlatformApiToken.objects.select_for_update().get(pk=request.platform_token.pk)
        replay = idempotency.existing_response()
        if replay:
            return replay
        approval = (
            PlatformApproval.objects.select_for_update()
            .select_related("task", "artifact", "requested_by", "decided_by")
            .filter(pk=approval_id)
            .first()
        )
        if not approval:
            raise PlatformApiError("not_found", "승인 요청을 찾을 수 없습니다.", status=404)
        if approval.status != PlatformApproval.Status.PENDING:
            if approval.status == decision:
                payload = {"data": approval_payload(approval)}
                idempotency.store(payload, 200)
                return JsonResponse(payload)
            raise PlatformApiError("approval_already_decided", "이미 다른 결정으로 처리되었습니다.", status=409)
        try:
            read_artifact(approval.artifact.artifact_path, approval.target_sha256)
        except ArtifactStorageError as exc:
            raise PlatformApiError(
                "approval_target_changed",
                "승인 대상 아티팩트가 변경되었거나 손상되었습니다.",
                status=409,
            ) from exc
        approval.status = decision
        approval.decision_note = note
        approval.decided_by = request.platform_token.agent
        approval.decided_at = timezone.now()
        approval.save(update_fields=["status", "decision_note", "decided_by", "decided_at"])
        transition_task(approval.task, decision, request.platform_token.agent)
        record_event(
            event_type=f"approval.{decision}",
            entity_type="approval",
            entity_id=approval.id,
            task=approval.task,
            actor=request.platform_token.agent,
            data={"artifact_id": str(approval.artifact_id), "target_sha256": approval.target_sha256},
        )
        payload = {"data": approval_payload(approval)}
        idempotency.store(payload, 200)
    return JsonResponse(payload)


@csrf_exempt
@require_GET
@platform_auth({"GET": "platform:read"})
def events(request: HttpRequest) -> JsonResponse:
    queryset = PlatformEvent.objects.select_related("actor", "task")
    if task_id := request.GET.get("task_id"):
        queryset = queryset.filter(task_id=uuid_value(task_id, "task_id"))
    if event_type := request.GET.get("event_type"):
        queryset = queryset.filter(event_type=event_type)
    return JsonResponse(list_payload(request, queryset, event_payload))


@csrf_exempt
@require_GET
@platform_auth({"GET": "platform:read"})
def workflow_detail(request: HttpRequest, task_id: UUID) -> JsonResponse:
    task = PlatformTask.objects.select_related("created_by", "inbox_item").filter(pk=task_id).first()
    if not task:
        raise PlatformApiError("not_found", "작업을 찾을 수 없습니다.", status=404)
    return JsonResponse(
        {
            "data": {
                "task_id": str(task.id),
                "status": task.status,
                "status_label": task.get_status_display(),
                "allowed_status_transitions": sorted(TASK_TRANSITIONS.get(task.status, set())),
                "events": [event_payload(item) for item in task.events.select_related("actor")],
            }
        }
    )


@csrf_exempt
@require_GET
@platform_auth({"GET": "platform:read"})
def search(request: HttpRequest) -> JsonResponse:
    query = request.GET.get("q", "").strip()
    if len(query) < 2:
        raise PlatformApiError("invalid_query", "검색어는 두 글자 이상이어야 합니다.")
    try:
        limit = min(max(int(request.GET.get("limit", "20")), 1), 50)
    except ValueError as exc:
        raise PlatformApiError("invalid_query", "limit은 정수여야 합니다.") from exc

    results = []
    tasks = PlatformTask.objects.filter(
        Q(title__icontains=query) | Q(description__icontains=query)
    ).order_by("-updated_at")[:limit]
    for task in tasks:
        results.append(
            {
                "type": "task",
                "id": str(task.id),
                "title": task.title,
                "excerpt": task.description[:300],
                "status": task.status,
                "updated_at": iso(task.updated_at),
            }
        )
    remaining = limit - len(results)
    if remaining:
        knowledge = KnowledgeItem.objects.filter(hidden_at__isnull=True).filter(
            Q(title__icontains=query)
            | Q(summary__icontains=query)
            | Q(question__icontains=query)
            | Q(answer__icontains=query)
        ).order_by("-generated_at")[:remaining]
        for item in knowledge:
            results.append(
                {
                    "type": "knowledge",
                    "id": str(item.id),
                    "title": item.title,
                    "excerpt": (item.summary or item.question or item.answer)[:300],
                    "status": item.status,
                    "source_url": item.slack_source_url,
                    "updated_at": iso(item.updated_at),
                }
            )
    remaining = limit - len(results)
    if remaining:
        artifacts = PlatformArtifact.objects.filter(title__icontains=query).order_by("-created_at")[:remaining]
        for artifact in artifacts:
            results.append(
                {
                    "type": "artifact",
                    "id": str(artifact.id),
                    "task_id": str(artifact.task_id),
                    "title": artifact.title,
                    "excerpt": "",
                    "status": artifact.kind,
                    "updated_at": iso(artifact.created_at),
                }
            )
    return JsonResponse({"data": results, "meta": {"query": query, "count": len(results)}})
