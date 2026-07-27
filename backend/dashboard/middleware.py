import re

from django.conf import settings
from django.http import JsonResponse
from django.middleware.csrf import CsrfViewMiddleware

from .access import (
    authentication_required_response,
    internal_service_authenticated,
)


CONSUMPTION_STATE_WRITE_PATH = re.compile(
    r"^/api/(?:knowledge|runs)/\d+/state/$"
)


class ConsumptionStateReadOnlyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            settings.CONSUMPTION_STATE_READ_ONLY
            and request.method in {"POST", "PATCH"}
            and CONSUMPTION_STATE_WRITE_PATH.fullmatch(request.path)
        ):
            response = JsonResponse(
                {"error": "consumption_state_read_only", "retryable": True},
                status=503,
            )
            response["Retry-After"] = "300"
            return response
        return self.get_response(request)


PUBLIC_LEGACY_API_PATHS = {
    "/api/csrf/",
    "/api/health/",
}


class DashboardAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.dashboard_internal_authenticated = False
        if not settings.DASHBOARD_AUTH_REQUIRED:
            return self.get_response(request)
        if request.path.startswith("/api/v1/"):
            return self.get_response(request)
        if request.path in PUBLIC_LEGACY_API_PATHS:
            return self.get_response(request)
        if not request.path.startswith("/api/"):
            return self.get_response(request)
        if request.user.is_authenticated:
            return self.get_response(request)
        if internal_service_authenticated(request):
            request.dashboard_internal_authenticated = True
            return self.get_response(request)
        return authentication_required_response()


class DashboardCsrfViewMiddleware(CsrfViewMiddleware):
    def process_view(self, request, callback, callback_args, callback_kwargs):
        if getattr(request, "dashboard_internal_authenticated", False):
            return None
        return super().process_view(
            request,
            callback,
            callback_args,
            callback_kwargs,
        )
