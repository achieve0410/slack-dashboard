import json
import os
import tempfile
from pathlib import Path

from django.test import Client, TestCase, override_settings

from .models import PlatformAgent, PlatformApiToken


class TokenManagementApiTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.token_root = Path(self.temporary.name) / "tokens"
        self.settings_override = override_settings(
            DASHBOARD_PLATFORM_TOKEN_ROOT=self.token_root,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.agent = PlatformAgent.objects.create(
            key="worker",
            name="Worker",
            capabilities=["research"],
        )

    def post(self, path: str, payload: dict):
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def issue(self, **overrides):
        payload = {
            "agent_key": self.agent.key,
            "scopes": ["platform:read", "tasks:write"],
            "expires_days": 30,
            **overrides,
        }
        response = self.post("/api/platform-tokens/", payload)
        self.assertEqual(response.status_code, 201, response.content)
        return response

    def test_collection_lists_metadata_without_exposing_secrets_or_hashes(self):
        record, raw_token = PlatformApiToken.issue(
            name="dashboard-platform",
            agent=self.agent,
            scopes=["platform:read"],
        )

        response = self.client.get("/api/platform-tokens/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["tokens"][0]["id"], record.pk)
        self.assertEqual(body["tokens"][0]["agent"]["key"], self.agent.key)
        self.assertIn("platform:read", body["available_scopes"])
        serialized = json.dumps(body)
        self.assertNotIn(raw_token, serialized)
        self.assertNotIn(record.token_hash, serialized)

    def test_issue_returns_secret_once_writes_private_file_and_rotates_existing(self):
        first = self.issue().json()
        first_raw = first["secret"]
        token_path = self.token_root / "worker.token"

        second = self.issue(scopes=["platform:read"]).json()
        second_raw = second["secret"]

        self.assertNotEqual(first_raw, second_raw)
        self.assertIsNone(PlatformApiToken.authenticate(first_raw))
        self.assertIsNotNone(PlatformApiToken.authenticate(second_raw))
        self.assertEqual(token_path.read_text().strip(), second_raw)
        self.assertEqual(os.stat(self.token_root).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(token_path).st_mode & 0o777, 0o600)
        listing = json.dumps(self.client.get("/api/platform-tokens/").json())
        self.assertNotIn(first_raw, listing)
        self.assertNotIn(second_raw, listing)

    def test_rotate_preserves_agent_and_can_change_scopes_and_expiry(self):
        issued = self.issue().json()
        token_id = issued["token"]["id"]

        response = self.post(
            f"/api/platform-tokens/{token_id}/rotate/",
            {"scopes": ["platform:read", "artifacts:write"], "expires_days": 7},
        )

        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["token"]["agent"]["key"], self.agent.key)
        self.assertEqual(body["token"]["scopes"], ["artifacts:write", "platform:read"])
        self.assertIsNotNone(body["token"]["expires_at"])
        self.assertIsNone(PlatformApiToken.authenticate(issued["secret"]))
        self.assertIsNotNone(PlatformApiToken.authenticate(body["secret"]))

    def test_revoke_invalidates_token_and_removes_matching_file(self):
        issued = self.issue().json()
        token_path = self.token_root / "worker.token"

        response = self.post(
            f"/api/platform-tokens/{issued['token']['id']}/revoke/",
            {},
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()["token"]["is_active"])
        self.assertIsNone(PlatformApiToken.authenticate(issued["secret"]))
        self.assertFalse(token_path.exists())
        self.assertNotIn("secret", response.json())

    def test_mutations_require_csrf_and_validate_scopes(self):
        csrf_client = Client(enforce_csrf_checks=True)
        payload = {
            "agent_key": self.agent.key,
            "scopes": ["platform:read"],
            "expires_days": 30,
        }
        blocked = csrf_client.post(
            "/api/platform-tokens/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        csrf_client.get("/api/csrf/")
        csrf_token = csrf_client.cookies["csrftoken"].value
        allowed = csrf_client.post(
            "/api/platform-tokens/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        invalid_scope = self.post(
            "/api/platform-tokens/",
            {**payload, "scopes": ["platform:read", "root:all"]},
        )

        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(allowed.status_code, 201)
        self.assertEqual(invalid_scope.status_code, 400)
        self.assertEqual(invalid_scope.json()["code"], "invalid_scopes")
