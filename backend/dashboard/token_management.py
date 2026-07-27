import json
import logging
import os
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .access import dashboard_staff_required
from .models import PlatformAgent, PlatformApiToken


logger = logging.getLogger(__name__)

TOKEN_NAME = "dashboard-platform"
AVAILABLE_SCOPES = (
    "platform:read",
    "inbox:write",
    "tasks:write",
    "artifacts:write",
    "approvals:request",
    "approvals:decide",
)


def error(code: str, message: str, *, status: int = 400) -> JsonResponse:
    return JsonResponse({"code": code, "error": message}, status=status)


def parse_json(request: HttpRequest) -> dict:
    try:
        value = json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        raise ValueError("올바른 JSON 요청이 아닙니다.") from exc
    if not isinstance(value, dict):
        raise ValueError("요청 본문은 JSON 객체여야 합니다.")
    return value


def token_file(agent_key: str) -> Path:
    root = Path(settings.DASHBOARD_PLATFORM_TOKEN_ROOT).expanduser().resolve()
    return root / f"{agent_key}.token"


def write_token_file(agent_key: str, raw_token: str) -> None:
    path = token_file(agent_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    file_descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as output:
        output.write(raw_token + "\n")
    os.chmod(path, 0o600)


def remove_matching_token_file(record: PlatformApiToken) -> None:
    path = token_file(record.agent.key)
    try:
        raw_token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    if PlatformApiToken.digest(raw_token) == record.token_hash:
        path.unlink()


def token_status(record: PlatformApiToken) -> str:
    if record.revoked_at:
        return "revoked"
    if not record.is_active:
        return "inactive"
    if not record.agent.is_active:
        return "agent_inactive"
    if record.expires_at and record.expires_at <= timezone.now():
        return "expired"
    return "active"


def token_payload(record: PlatformApiToken) -> dict:
    status = token_status(record)
    return {
        "id": record.pk,
        "name": record.name,
        "agent": {
            "key": record.agent.key,
            "name": record.agent.name,
        },
        "token_prefix": f"dpt_{record.token_prefix}_…",
        "scopes": record.scopes,
        "status": status,
        "is_active": status == "active",
        "expires_at": record.expires_at,
        "last_used_at": record.last_used_at,
        "created_at": record.created_at,
        "revoked_at": record.revoked_at,
        "file_present": token_file(record.agent.key).is_file(),
    }


def validate_issue_data(data: dict, *, require_agent: bool) -> tuple[PlatformAgent | None, list[str], object]:
    allowed = {"agent_key", "scopes", "expires_days"} if require_agent else {"scopes", "expires_days"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"지원하지 않는 필드입니다: {', '.join(unknown)}")

    agent = None
    if require_agent:
        agent_key = data.get("agent_key")
        if not isinstance(agent_key, str) or not agent_key:
            raise ValueError("agent_key가 필요합니다.")
        agent = PlatformAgent.objects.filter(key=agent_key, is_active=True).first()
        if not agent:
            raise LookupError("활성 에이전트를 찾을 수 없습니다.")

    scopes = data.get("scopes")
    if not isinstance(scopes, list) or not scopes or not all(isinstance(scope, str) for scope in scopes):
        raise ValueError("scopes는 하나 이상의 문자열 목록이어야 합니다.")
    normalized_scopes = sorted(set(scopes))
    invalid_scopes = sorted(set(normalized_scopes) - set(AVAILABLE_SCOPES))
    if invalid_scopes:
        raise PermissionError(", ".join(invalid_scopes))

    expires_days = data.get("expires_days")
    if expires_days is None:
        expires_at = None
    elif isinstance(expires_days, bool) or not isinstance(expires_days, int) or not 1 <= expires_days <= 3650:
        raise ValueError("expires_days는 1~3650 사이의 정수이거나 null이어야 합니다.")
    else:
        expires_at = timezone.now() + timedelta(days=expires_days)
    return agent, normalized_scopes, expires_at


def issue_token(*, agent: PlatformAgent, scopes: list[str], expires_at) -> tuple[PlatformApiToken, str]:
    with transaction.atomic():
        now = timezone.now()
        PlatformApiToken.objects.filter(
            agent=agent,
            name=TOKEN_NAME,
            is_active=True,
        ).update(is_active=False, revoked_at=now)
        record, raw_token = PlatformApiToken.issue(
            name=TOKEN_NAME,
            agent=agent,
            scopes=scopes,
            expires_at=expires_at,
        )
        write_token_file(agent.key, raw_token)
    logger.info("platform token issued agent=%s token_id=%s", agent.key, record.pk)
    return record, raw_token


@require_http_methods(["GET", "POST"])
@dashboard_staff_required
def platform_tokens(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        tokens = PlatformApiToken.objects.select_related("agent").all()
        agents = PlatformAgent.objects.filter(is_active=True).order_by("key")
        return JsonResponse(
            {
                "tokens": [token_payload(token) for token in tokens],
                "agents": [
                    {
                        "key": agent.key,
                        "name": agent.name,
                        "capabilities": agent.capabilities,
                    }
                    for agent in agents
                ],
                "available_scopes": list(AVAILABLE_SCOPES),
            }
        )

    try:
        data = parse_json(request)
        agent, scopes, expires_at = validate_issue_data(data, require_agent=True)
    except PermissionError as exc:
        return error("invalid_scopes", f"지원하지 않는 권한 범위입니다: {exc}")
    except LookupError as exc:
        return error("agent_not_found", str(exc), status=404)
    except ValueError as exc:
        return error("invalid_request", str(exc))

    record, raw_token = issue_token(
        agent=agent,
        scopes=scopes,
        expires_at=expires_at,
    )
    return JsonResponse(
        {
            "token": token_payload(record),
            "secret": raw_token,
            "secret_visible_once": True,
        },
        status=201,
    )


@require_POST
@dashboard_staff_required
def rotate_platform_token(request: HttpRequest, token_id: int) -> JsonResponse:
    current = get_object_or_404(
        PlatformApiToken.objects.select_related("agent"),
        pk=token_id,
    )
    try:
        data = parse_json(request)
        _, scopes, expires_at = validate_issue_data(data, require_agent=False)
    except PermissionError as exc:
        return error("invalid_scopes", f"지원하지 않는 권한 범위입니다: {exc}")
    except ValueError as exc:
        return error("invalid_request", str(exc))

    record, raw_token = issue_token(
        agent=current.agent,
        scopes=scopes,
        expires_at=expires_at,
    )
    return JsonResponse(
        {
            "token": token_payload(record),
            "secret": raw_token,
            "secret_visible_once": True,
        },
        status=201,
    )


@require_POST
@dashboard_staff_required
def revoke_platform_token(request: HttpRequest, token_id: int) -> JsonResponse:
    record = get_object_or_404(
        PlatformApiToken.objects.select_related("agent"),
        pk=token_id,
    )
    try:
        data = parse_json(request)
    except ValueError as exc:
        return error("invalid_request", str(exc))
    if data:
        return error("invalid_request", "폐기 요청 본문은 비어 있어야 합니다.")

    remove_matching_token_file(record)
    if record.is_active or not record.revoked_at:
        record.is_active = False
        record.revoked_at = timezone.now()
        record.save(update_fields=["is_active", "revoked_at"])
    logger.info("platform token revoked agent=%s token_id=%s", record.agent.key, record.pk)
    return JsonResponse({"token": token_payload(record)})
