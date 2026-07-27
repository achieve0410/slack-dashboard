import json
import threading
import time
from datetime import UTC, datetime
from unittest.mock import patch

from django.db import close_old_connections, connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils.dateparse import parse_datetime

from . import views as dashboard_views
from .models import (
    ContentRun,
    CronJob,
    KnowledgeConsumptionState,
    KnowledgeItem,
    UserResponse,
    UserRunState,
)
from .services import reconcile_cron_runs


class KnowledgeConsumptionStateApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        job = CronJob.objects.create(external_id="state-cron", name="상태 Cron")
        self.run = ContentRun.objects.create(
            job=job,
            status=ContentRun.Status.SUCCESS,
            title="Cron 상태 항목",
            body="본문",
            generated_at=datetime(2026, 7, 17, tzinfo=UTC),
        )
        reconcile_cron_runs([self.run.pk])
        self.cron_item = KnowledgeItem.objects.get(content_run=self.run)
        self.slack_item = KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key="slack:state:1",
            status=KnowledgeItem.Status.PENDING,
            title="Slack 상태 항목",
            summary="요약",
            question="질문",
            answer="답변",
            source_hash="a" * 64,
            generated_at=datetime(2026, 7, 17, 1, tzinfo=UTC),
        )

    @staticmethod
    def patch(client: Client, url: str, data) -> object:
        return client.patch(
            url,
            data=json.dumps(data),
            content_type="application/json",
        )

    def state_url(self, item: KnowledgeItem | None = None) -> str:
        return f"/api/knowledge/{(item or self.cron_item).pk}/state/"

    def test_fresh_get_requests_do_not_write_or_create_sessions(self):
        urls = (
            "/api/summary/",
            "/api/runs/",
            f"/api/runs/{self.run.pk}/",
            f"/api/knowledge/{self.slack_item.pk}/",
        )

        for url in urls:
            with self.subTest(url=url), CaptureQueriesContext(connection) as queries:
                response = Client().get(url)

            self.assertEqual(response.status_code, 200)
            writes = [
                query["sql"]
                for query in queries
                if query["sql"].lstrip().upper().startswith(
                    ("INSERT", "UPDATE", "DELETE")
                )
            ]
            self.assertEqual(writes, [])

    def test_response_post_creates_and_reuses_owner_session(self):
        client = Client()
        url = f"/api/runs/{self.run.pk}/responses/"

        first = client.post(
            url,
            data=json.dumps({"question_key": "first", "answer": "첫 답변"}),
            content_type="application/json",
        )
        second = client.post(
            url,
            data=json.dumps({"question_key": "second", "answer": "둘째 답변"}),
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        responses = list(UserResponse.objects.order_by("id"))
        self.assertEqual(len(responses), 2)
        self.assertTrue(responses[0].session_key)
        self.assertEqual(responses[0].session_key, responses[1].session_key)
        self.assertCountEqual(
            [
                item["id"]
                for item in client.get(f"/api/runs/{self.run.pk}/").json()["responses"]
            ],
            [responses[0].pk, responses[1].pk],
        )
        self.assertEqual(
            Client().get(f"/api/runs/{self.run.pk}/").json()["responses"],
            [],
        )

    def test_absent_state_is_global_and_serialized_for_both_sources(self):
        expected = {
            "read": False,
            "bookmarked": False,
            "completed": False,
            "archived": False,
            "read_at": None,
            "bookmarked_at": None,
            "completed_at": None,
            "archived_at": None,
            "note": "",
            "created_at": None,
            "updated_at": None,
        }

        cron = self.client.get(self.state_url()).json()
        slack = self.client.get(self.state_url(self.slack_item)).json()
        cron_detail = self.client.get(f"/api/runs/{self.run.pk}/").json()["state"]
        slack_detail = self.client.get(
            f"/api/knowledge/{self.slack_item.pk}/"
        ).json()["state"]

        self.assertEqual(cron, expected)
        self.assertEqual(slack, expected)
        self.assertEqual(cron_detail, expected)
        self.assertEqual(slack_detail, expected)
        self.assertEqual(KnowledgeConsumptionState.objects.count(), 0)

    def test_transitions_are_independent_and_same_value_retry_has_no_update(self):
        first = self.patch(
            self.client,
            self.state_url(),
            {"read": True, "bookmarked": True, "note": "  줄 1\n줄 2  "},
        )
        self.assertEqual(first.status_code, 200)
        payload = first.json()
        self.assertTrue(payload["read"])
        self.assertTrue(payload["bookmarked"])
        self.assertFalse(payload["completed"])
        self.assertFalse(payload["archived"])
        self.assertEqual(payload["note"], "  줄 1\n줄 2  ")
        updated_at = payload["updated_at"]
        read_at = payload["read_at"]
        bookmarked_at = payload["bookmarked_at"]

        with CaptureQueriesContext(connection) as queries:
            repeated = self.patch(
                self.client,
                self.state_url(),
                {"read": True, "bookmarked": True, "note": "  줄 1\n줄 2  "},
            )

        self.assertEqual(repeated.json()["updated_at"], updated_at)
        self.assertEqual(repeated.json()["read_at"], read_at)
        self.assertEqual(repeated.json()["bookmarked_at"], bookmarked_at)
        self.assertFalse(
            any(query["sql"].lstrip().upper().startswith("UPDATE") for query in queries)
        )

        completed = self.patch(
            self.client,
            self.state_url(),
            {"completed": True},
        ).json()
        self.assertTrue(completed["completed"])
        self.assertFalse(completed["archived"])
        archived = self.patch(
            self.client,
            self.state_url(),
            {"archived": True},
        ).json()
        self.assertTrue(archived["completed"])
        self.assertTrue(archived["archived"])
        cleared = self.patch(
            self.client,
            self.state_url(),
            {"read": False},
        ).json()
        self.assertFalse(cleared["read"])
        self.assertIsNone(cleared["read_at"])
        self.assertTrue(cleared["bookmarked"])

    def test_strict_patch_rejects_invalid_values_without_writes(self):
        invalid_requests = (
            ({"unknown": True}, "지원하지 않는 필드"),
            ({"read": "true"}, "JSON true 또는 false"),
            ({"bookmarked": 1}, "JSON true 또는 false"),
            ({"note": 123}, "문자열"),
            ({"note": "x" * 5001}, "5,000자"),
            (["read"], "JSON 객체"),
        )
        for data, message in invalid_requests:
            with self.subTest(data_type=type(data).__name__, message=message):
                response = self.patch(self.client, self.state_url(), data)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["error"])
                self.assertEqual(KnowledgeConsumptionState.objects.count(), 0)

        accepted = self.patch(
            self.client,
            self.state_url(),
            {"note": "x" * 5000},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(len(accepted.json()["note"]), 5000)

    def test_absent_false_empty_patch_does_not_create_row(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.patch(
                self.client,
                self.state_url(),
                {
                    "read": False,
                    "bookmarked": False,
                    "completed": False,
                    "archived": False,
                    "note": "",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(KnowledgeConsumptionState.objects.count(), 0)
        self.assertFalse(
            any(query["sql"].lstrip().upper().startswith("INSERT") for query in queries)
        )

    def test_archive_excludes_default_lists_but_not_detail_or_state_patch(self):
        self.patch(self.client, self.state_url(), {"completed": True})
        active_ids = {
            item["id"] for item in self.client.get("/api/knowledge/").json()["results"]
        }
        self.assertIn(self.cron_item.pk, active_ids)

        self.patch(self.client, self.state_url(), {"archived": True})
        archived_ids = {
            item["id"] for item in self.client.get("/api/knowledge/").json()["results"]
        }
        self.assertNotIn(self.cron_item.pk, archived_ids)
        self.assertEqual(self.client.get("/api/summary/").json()["progress"]["completed"], 1)
        self.assertNotIn(
            self.run.pk,
            [item["id"] for item in self.client.get("/api/runs/").json()["results"]],
        )
        self.assertEqual(
            self.client.get(f"/api/knowledge/{self.cron_item.pk}/").status_code,
            200,
        )
        self.assertEqual(self.client.get(f"/api/runs/{self.run.pk}/").status_code, 200)

        restored = self.patch(
            self.client,
            self.state_url(),
            {"archived": False},
        )
        self.assertEqual(restored.status_code, 200)
        self.assertTrue(restored.json()["completed"])
        self.assertFalse(restored.json()["archived"])

    def test_state_is_shared_across_clients_and_legacy_route_bridges_to_canonical(self):
        other = Client()
        saved = self.patch(
            self.client,
            self.state_url(self.slack_item),
            {"bookmarked": True, "note": "공유 메모"},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(
            other.get(self.state_url(self.slack_item)).json()["note"], "공유 메모"
        )

        legacy = other.post(
            f"/api/runs/{self.run.pk}/state/",
            data=json.dumps({"completed": True}),
            content_type="application/json",
        )
        self.assertEqual(legacy.status_code, 200)
        self.assertTrue(legacy.json()["completed"])
        self.assertTrue(
            KnowledgeConsumptionState.objects.get(
                knowledge_item=self.cron_item
            ).completed_at
        )
        self.assertEqual(UserRunState.objects.count(), 0)

        bookmarked = self.client.get("/api/knowledge/?bookmarked=1").json()["results"]
        self.assertEqual([item["id"] for item in bookmarked], [self.slack_item.pk])

    def test_hidden_and_orphan_legacy_writes_are_rejected(self):
        self.cron_item.hidden_at = datetime(2026, 7, 17, 2, tzinfo=UTC)
        self.cron_item.save(update_fields=["hidden_at"])
        self.assertEqual(
            self.patch(self.client, self.state_url(), {"read": True}).status_code,
            404,
        )

        orphan = ContentRun.objects.create(
            job=self.run.job,
            status=ContentRun.Status.FAILED,
            title="orphan",
            generated_at=datetime(2026, 7, 17, 3, tzinfo=UTC),
        )
        self.assertEqual(
            self.patch(
                self.client,
                f"/api/runs/{orphan.pk}/state/",
                {"completed": True},
            ).status_code,
            404,
        )
        self.assertEqual(UserRunState.objects.count(), 0)

    def test_state_patch_does_not_mutate_classification_or_source_fields(self):
        fields = (
            "source_type",
            "source_key",
            "status",
            "category_id",
            "classification_model",
            "classification_confidence",
            "classification_reason",
            "classified_at",
            "reviewed_by_id",
            "reviewed_at",
            "hidden_at",
        )
        before = tuple(getattr(self.slack_item, field) for field in fields)

        response = self.patch(
            self.client,
            self.state_url(self.slack_item),
            {"read": True, "completed": True},
        )

        self.assertEqual(response.status_code, 200)
        self.slack_item.refresh_from_db()
        self.assertEqual(before, tuple(getattr(self.slack_item, field) for field in fields))

    @override_settings(CONSUMPTION_STATE_READ_ONLY=True)
    def test_read_only_gate_blocks_canonical_and_legacy_writes(self):
        state = KnowledgeConsumptionState.objects.create(
            knowledge_item=self.cron_item,
            note="보존",
        )
        legacy_state = UserRunState.objects.create(
            run=self.run,
            session_key="legacy",
            note="legacy 보존",
        )
        before = tuple(
            KnowledgeConsumptionState.objects.filter(pk=state.pk).values_list(
                "read_at",
                "bookmarked_at",
                "completed_at",
                "archived_at",
                "note",
                "created_at",
                "updated_at",
            )[0]
        )

        csrf_client = Client(enforce_csrf_checks=True)
        for url in (self.state_url(), f"/api/runs/{self.run.pk}/state/"):
            with self.subTest(url=url):
                response = self.patch(csrf_client, url, {"completed": True})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(
                    response.json(),
                    {"error": "consumption_state_read_only", "retryable": True},
                )
                self.assertEqual(response["Retry-After"], "300")

        after = tuple(
            KnowledgeConsumptionState.objects.filter(pk=state.pk).values_list(
                "read_at",
                "bookmarked_at",
                "completed_at",
                "archived_at",
                "note",
                "created_at",
                "updated_at",
            )[0]
        )
        self.assertEqual(before, after)
        legacy_state.refresh_from_db()
        self.assertEqual(legacy_state.note, "legacy 보존")
        self.assertFalse(legacy_state.completed)
        self.assertEqual(self.client.get(self.state_url()).status_code, 200)
        self.assertEqual(self.client.get(f"/api/runs/{self.run.pk}/").status_code, 200)


class KnowledgeConsumptionStateConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        job = CronJob.objects.create(
            external_id="state-concurrency",
            name="상태 동시성",
        )
        run = ContentRun.objects.create(
            job=job,
            status=ContentRun.Status.SUCCESS,
            title="상태 동시성 항목",
            body="본문",
            generated_at=datetime(2026, 7, 17, tzinfo=UTC),
        )
        self.item = KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.CRON,
            source_key=f"cron:{run.pk}",
            content_run=run,
            status=KnowledgeItem.Status.PENDING,
            title=run.title,
            summary=run.body,
            source_hash="c" * 64,
            generated_at=run.generated_at,
        )
        KnowledgeConsumptionState.objects.create(
            knowledge_item=self.item,
            note="initial",
        )

    def test_concurrent_patch_last_committed_write_wins_mysql(self):
        if connection.vendor != "mysql":
            self.skipTest("MySQL row-lock concurrent PATCH regression")

        first_payload_ready = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        results = {}
        errors = []
        original_state_payload = dashboard_views.state_payload

        def blocking_state_payload(state):
            payload = original_state_payload(state)
            if threading.current_thread().name == "first-patch":
                first_payload_ready.set()
                if not release_first.wait(10):
                    raise TimeoutError("first PATCH release timed out")
            return payload

        def write_note(name, note):
            close_old_connections()
            try:
                if name == "second":
                    second_started.set()
                response = Client().patch(
                    f"/api/knowledge/{self.item.pk}/state/",
                    data=json.dumps({"note": note}),
                    content_type="application/json",
                )
                if response.status_code != 200:
                    raise AssertionError(response.content.decode())
                results[name] = response.json()
            except Exception as error:
                errors.append(error)
            finally:
                connections["default"].close()

        with patch.object(
            dashboard_views,
            "state_payload",
            side_effect=blocking_state_payload,
        ):
            first = threading.Thread(
                target=write_note,
                args=("first", "first committed"),
                name="first-patch",
            )
            second = threading.Thread(
                target=write_note,
                args=("second", "last committed"),
                name="second-patch",
            )
            first.start()
            self.assertTrue(first_payload_ready.wait(10))
            second.start()
            self.assertTrue(second_started.wait(10))
            time.sleep(0.2)
            self.assertTrue(second.is_alive())
            release_first.set()
            first.join(10)
            second.join(10)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results["first"]["note"], "first committed")
        self.assertEqual(results["second"]["note"], "last committed")
        self.assertGreater(
            parse_datetime(results["second"]["updated_at"]),
            parse_datetime(results["first"]["updated_at"]),
        )

        state = KnowledgeConsumptionState.objects.get(knowledge_item=self.item)
        self.assertEqual(state.note, "last committed")
        self.assertEqual(
            parse_datetime(results["second"]["updated_at"]),
            state.updated_at.replace(
                microsecond=state.updated_at.microsecond // 1000 * 1000
            ),
        )
