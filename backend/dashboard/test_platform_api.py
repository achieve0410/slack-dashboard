import json
import os
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (
    PlatformAgent,
    PlatformApiToken,
    PlatformApproval,
    PlatformArtifact,
    PlatformEvent,
    PlatformTask,
)


class PlatformApiTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.artifact_root = Path(self.temporary.name) / "artifacts"
        self.settings_override = override_settings(
            DASHBOARD_PLATFORM_ARTIFACT_ROOT=self.artifact_root,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

        self.worker = PlatformAgent.objects.create(
            key="worker",
            name="Worker",
            capabilities=["research", "analysis"],
        )
        self.manager = PlatformAgent.objects.create(
            key="manager",
            name="Manager",
            capabilities=["review", "approval"],
        )
        self.worker_token, self.worker_raw_token = PlatformApiToken.issue(
            name="worker-token",
            agent=self.worker,
            scopes=[
                "platform:read",
                "inbox:write",
                "tasks:write",
                "artifacts:write",
                "approvals:request",
            ],
        )
        self.manager_token, self.manager_raw_token = PlatformApiToken.issue(
            name="manager-token",
            agent=self.manager,
            scopes=["platform:read", "approvals:decide"],
        )

    @staticmethod
    def auth(token: str) -> dict[str, str]:
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def post(
        self,
        url: str,
        payload: dict,
        *,
        token: str | None = None,
        idempotency_key: str | None = None,
    ):
        headers = self.auth(token or self.worker_raw_token)
        if idempotency_key:
            headers["HTTP_IDEMPOTENCY_KEY"] = idempotency_key
        return self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            **headers,
        )

    def create_task(self, *, key: str = "task-create") -> dict:
        response = self.post(
            "/api/v1/tasks/",
            {"title": "플랫폼 API 구축", "description": "공통 계약을 구현합니다."},
            idempotency_key=key,
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["data"]

    def create_artifact(self, task_id: str, *, key: str = "artifact-create") -> dict:
        response = self.post(
            "/api/v1/artifacts/",
            {
                "task_id": task_id,
                "kind": "analysis",
                "title": "분석 결과",
                "content": "첫 번째 분석 결과입니다.",
                "mime_type": "text/markdown",
            },
            idempotency_key=key,
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["data"]

    def test_bearer_token_is_required_and_scopes_are_enforced(self):
        missing = self.client.get("/api/v1/tasks/")
        invalid = self.client.get(
            "/api/v1/tasks/",
            **self.auth("dpt_invalid_invalid"),
        )
        write_only_token, raw_write_only = PlatformApiToken.issue(
            name="write-only",
            agent=self.worker,
            scopes=["tasks:write"],
        )
        forbidden = self.client.get(
            "/api/v1/tasks/",
            **self.auth(raw_write_only),
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(forbidden.status_code, 403)
        self.assertNotIn(self.worker_raw_token, self.worker_token.token_hash)
        self.assertNotIn(raw_write_only, write_only_token.token_hash)

    def test_expired_token_is_rejected(self):
        _, raw_token = PlatformApiToken.issue(
            name="expired",
            agent=self.worker,
            scopes=["platform:read"],
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        response = self.client.get("/api/v1/tasks/", **self.auth(raw_token))

        self.assertEqual(response.status_code, 401)

    def test_token_command_writes_private_file_and_rotates_existing_token(self):
        token_path = Path(self.temporary.name) / "tokens" / "employee.token"
        arguments = {
            "agent_key": "employee",
            "agent_name": "Employee",
            "token_name": "dashboard-platform",
            "scopes": "platform:read,tasks:write",
            "capabilities": "research",
            "output": str(token_path),
            "stdout": StringIO(),
        }
        call_command("issue_platform_token", **arguments)
        first_raw = token_path.read_text().strip()
        first_record = PlatformApiToken.authenticate(first_raw)

        call_command("issue_platform_token", **arguments)
        second_raw = token_path.read_text().strip()

        self.assertIsNotNone(first_record)
        self.assertNotEqual(first_raw, second_raw)
        self.assertIsNone(PlatformApiToken.authenticate(first_raw))
        self.assertIsNotNone(PlatformApiToken.authenticate(second_raw))
        self.assertEqual(os.stat(token_path).st_mode & 0o777, 0o600)

    def test_task_creation_requires_idempotency_and_replays_same_response(self):
        missing_key = self.post(
            "/api/v1/tasks/",
            {"title": "멱등성 없는 요청"},
        )
        first = self.post(
            "/api/v1/tasks/",
            {"title": "동일 작업", "description": "한 번만 생성"},
            idempotency_key="same-task",
        )
        repeated = self.post(
            "/api/v1/tasks/",
            {"title": "동일 작업", "description": "한 번만 생성"},
            idempotency_key="same-task",
        )
        conflict = self.post(
            "/api/v1/tasks/",
            {"title": "다른 작업"},
            idempotency_key="same-task",
        )

        self.assertEqual(missing_key.status_code, 428)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(repeated.status_code, 201)
        self.assertEqual(repeated.headers["Idempotency-Replayed"], "true")
        self.assertEqual(first.json(), repeated.json())
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(PlatformTask.objects.count(), 1)
        task = PlatformTask.objects.get()
        self.assertEqual(task.created_by, self.worker)
        self.assertTrue(
            PlatformEvent.objects.filter(
                task=task,
                event_type="task.created",
                actor=self.worker,
            ).exists()
        )

    def test_artifacts_are_immutable_files_with_monotonic_versions(self):
        task = self.create_task()
        first = self.create_artifact(task["id"])
        second_response = self.post(
            "/api/v1/artifacts/",
            {
                "task_id": task["id"],
                "series_id": first["series_id"],
                "kind": "analysis",
                "title": "분석 결과 수정본",
                "content": "두 번째 분석 결과입니다.",
                "mime_type": "text/markdown",
            },
            idempotency_key="artifact-revision-2",
        )

        self.assertEqual(second_response.status_code, 201, second_response.content)
        second = second_response.json()["data"]
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(first["series_id"], second["series_id"])
        self.assertNotEqual(first["id"], second["id"])
        records = list(PlatformArtifact.objects.order_by("version"))
        self.assertEqual(len(records), 2)
        self.assertEqual(Path(records[0].artifact_path).read_text(), "첫 번째 분석 결과입니다.")
        self.assertEqual(Path(records[1].artifact_path).read_text(), "두 번째 분석 결과입니다.")
        self.assertTrue(str(Path(records[0].artifact_path).resolve()).startswith(str(self.artifact_root.resolve())))
        self.assertEqual(os.stat(self.artifact_root).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(Path(records[0].artifact_path).parent).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(records[0].artifact_path).st_mode & 0o777, 0o600)

        mutation = self.client.patch(
            f"/api/v1/artifacts/{first['id']}/",
            data=json.dumps({"content": "덮어쓰기"}),
            content_type="application/json",
            **self.auth(self.worker_raw_token),
        )
        self.assertEqual(mutation.status_code, 405)

    def test_approval_is_bound_to_artifact_hash_and_requires_decision_scope(self):
        task = self.create_task()
        artifact = self.create_artifact(task["id"])
        requested = self.post(
            "/api/v1/approvals/",
            {
                "task_id": task["id"],
                "artifact_id": artifact["id"],
                "note": "관리자 검토 요청",
            },
            idempotency_key="approval-request",
        )
        self.assertEqual(requested.status_code, 201, requested.content)
        approval = requested.json()["data"]
        self.assertEqual(approval["target_sha256"], artifact["sha256"])
        self.assertEqual(PlatformTask.objects.get(pk=task["id"]).status, "needs_review")

        duplicate = self.post(
            "/api/v1/approvals/",
            {"task_id": task["id"], "artifact_id": artifact["id"]},
            idempotency_key="approval-request-duplicate",
        )
        self.assertEqual(duplicate.status_code, 409)

        forbidden = self.post(
            f"/api/v1/approvals/{approval['id']}/decision/",
            {"decision": "approved", "note": "승인"},
            token=self.worker_raw_token,
            idempotency_key="approval-decision-worker",
        )
        decided = self.post(
            f"/api/v1/approvals/{approval['id']}/decision/",
            {"decision": "approved", "note": "검토 완료"},
            token=self.manager_raw_token,
            idempotency_key="approval-decision-manager",
        )

        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(decided.status_code, 200, decided.content)
        record = PlatformApproval.objects.get(pk=approval["id"])
        self.assertEqual(record.status, "approved")
        self.assertEqual(record.decided_by, self.manager)
        self.assertEqual(PlatformTask.objects.get(pk=task["id"]).status, "approved")
        self.assertTrue(
            PlatformEvent.objects.filter(
                task_id=task["id"],
                event_type="approval.approved",
                actor=self.manager,
            ).exists()
        )

    def test_approval_rejects_artifact_file_tampering(self):
        task = self.create_task()
        artifact = self.create_artifact(task["id"])
        requested = self.post(
            "/api/v1/approvals/",
            {"task_id": task["id"], "artifact_id": artifact["id"]},
            idempotency_key="tamper-approval-request",
        )
        self.assertEqual(requested.status_code, 201)
        record = PlatformArtifact.objects.get(pk=artifact["id"])
        Path(record.artifact_path).write_text("승인 후 변조된 내용")

        response = self.post(
            f"/api/v1/approvals/{requested.json()['data']['id']}/decision/",
            {"decision": "approved"},
            token=self.manager_raw_token,
            idempotency_key="tamper-approval-decision",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "approval_target_changed")
        self.assertEqual(PlatformApproval.objects.get().status, "pending")

    def test_task_context_search_and_event_history_are_available(self):
        task = self.create_task()
        artifact = self.create_artifact(task["id"])

        context = self.client.get(
            f"/api/v1/tasks/{task['id']}/context/",
            **self.auth(self.worker_raw_token),
        )
        search = self.client.get(
            "/api/v1/search/?q=%ED%94%8C%EB%9E%AB%ED%8F%BC",
            **self.auth(self.worker_raw_token),
        )
        events = self.client.get(
            f"/api/v1/events/?task_id={task['id']}",
            **self.auth(self.worker_raw_token),
        )

        self.assertEqual(context.status_code, 200, context.content)
        context_data = context.json()["data"]
        self.assertEqual(context_data["task"]["id"], task["id"])
        self.assertEqual(context_data["artifacts"][0]["id"], artifact["id"])
        self.assertEqual(context_data["artifacts"][0]["content"], "첫 번째 분석 결과입니다.")
        self.assertEqual(search.status_code, 200, search.content)
        self.assertTrue(any(item["type"] == "task" for item in search.json()["data"]))
        self.assertEqual(events.status_code, 200, events.content)
        self.assertGreaterEqual(len(events.json()["data"]), 2)
