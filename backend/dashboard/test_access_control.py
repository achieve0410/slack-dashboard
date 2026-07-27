import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import PlatformAgent


AUTH_SETTINGS = {
    "DASHBOARD_AUTH_REQUIRED": True,
    "DASHBOARD_INTERNAL_API_TOKEN": "internal-service-secret",
}


@override_settings(**AUTH_SETTINGS)
class DashboardAccessControlTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="viewer",
            password="viewer-password",
        )
        self.staff = get_user_model().objects.create_user(
            username="owner",
            password="owner-password",
            is_staff=True,
            is_superuser=True,
        )

    def test_health_csrf_and_login_remain_public(self):
        self.assertEqual(self.client.get("/api/health/").status_code, 200)
        self.assertEqual(self.client.get("/api/csrf/").status_code, 200)
        self.assertEqual(self.client.get("/accounts/login/").status_code, 200)

    def test_anonymous_legacy_api_is_rejected_with_json_401(self):
        response = self.client.get("/api/summary/")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "authentication_required")

    def test_frontend_redirects_anonymous_user_to_login(self):
        response = self.client.get("/knowledge/123")

        self.assertRedirects(
            response,
            "/accounts/login/?next=/knowledge/123",
            fetch_redirect_response=False,
        )

    def test_authenticated_session_can_open_frontend_and_legacy_api(self):
        self.client.force_login(self.user)

        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/summary/").status_code, 200)

    def test_internal_service_token_can_call_legacy_api(self):
        response = self.client.get(
            "/api/summary/",
            HTTP_AUTHORIZATION="Bearer internal-service-secret",
        )

        self.assertEqual(response.status_code, 200)

    def test_invalid_internal_service_token_is_rejected(self):
        response = self.client.get(
            "/api/summary/",
            HTTP_AUTHORIZATION="Bearer wrong-secret",
        )

        self.assertEqual(response.status_code, 401)

    def test_platform_v1_keeps_its_existing_bearer_authentication(self):
        response = self.client.get("/api/v1/tasks/")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["error"]["code"],
            "authentication_required",
        )

    def test_token_management_requires_staff_or_internal_service_token(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/platform-tokens/").status_code, 403)

        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/api/platform-tokens/").status_code, 200)

        service_response = Client().get(
            "/api/platform-tokens/",
            HTTP_AUTHORIZATION="Bearer internal-service-secret",
        )
        self.assertEqual(service_response.status_code, 200)

    def test_session_mutations_require_csrf_but_service_token_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            with override_settings(
                DASHBOARD_PLATFORM_TOKEN_ROOT=Path(directory) / "tokens"
            ):
                PlatformAgent.objects.create(
                    key="worker",
                    name="Worker",
                    capabilities=["research"],
                )
                payload = json.dumps(
                    {
                        "agent_key": "worker",
                        "scopes": ["platform:read"],
                        "expires_days": 30,
                    }
                )

                browser = Client(enforce_csrf_checks=True)
                browser.force_login(self.staff)
                blocked = browser.post(
                    "/api/platform-tokens/",
                    data=payload,
                    content_type="application/json",
                )
                service = Client(enforce_csrf_checks=True)
                allowed = service.post(
                    "/api/platform-tokens/",
                    data=payload,
                    content_type="application/json",
                    HTTP_AUTHORIZATION="Bearer internal-service-secret",
                )

        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(allowed.status_code, 201, allowed.content)
