import hmac
from functools import wraps

from django.conf import settings
from django.http import HttpRequest, JsonResponse


def internal_service_authenticated(request: HttpRequest) -> bool:
    expected = settings.DASHBOARD_INTERNAL_API_TOKEN
    if not expected:
        return False
    authorization = request.headers.get("Authorization", "")
    scheme, _, raw_token = authorization.partition(" ")
    return (
        scheme.casefold() == "bearer"
        and bool(raw_token)
        and hmac.compare_digest(raw_token.strip(), expected)
    )


def authentication_required_response() -> JsonResponse:
    response = JsonResponse(
        {
            "code": "authentication_required",
            "error": "로그인이 필요합니다.",
        },
        status=401,
    )
    response["WWW-Authenticate"] = 'Bearer realm="dashboard-internal"'
    return response


def staff_required_response(request: HttpRequest) -> JsonResponse | None:
    if not settings.DASHBOARD_AUTH_REQUIRED:
        return None
    if getattr(request, "dashboard_internal_authenticated", False):
        return None
    if not request.user.is_authenticated:
        return authentication_required_response()
    if not request.user.is_active or not request.user.is_staff:
        return JsonResponse(
            {
                "code": "permission_denied",
                "error": "관리자 권한이 필요합니다.",
            },
            status=403,
        )
    return None


def dashboard_staff_required(view):
    @wraps(view)
    def wrapped(request: HttpRequest, *args, **kwargs):
        denied = staff_required_response(request)
        if denied:
            return denied
        return view(request, *args, **kwargs)

    return wrapped
