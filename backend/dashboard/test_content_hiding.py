import json
from datetime import UTC, datetime

from django.test import Client, TestCase

from .classification import eligible_pending_items
from .models import ContentRun, CronJob, FreeQuestionMessage, KnowledgeItem
from .services import reconcile_cron_runs, reconcile_slack_thread


class ContentHidingApiTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)

    def delete(self, url: str):
        self.client.get("/api/csrf/")
        return self.client.delete(
            url,
            data=json.dumps({}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value,
        )

    def test_hides_slack_question_but_preserves_messages_and_suppression(self):
        thread_ts = "800.100"
        FreeQuestionMessage.objects.create(
            external_ts="800.100",
            thread_ts=thread_ts,
            role=FreeQuestionMessage.Role.USER,
            content="삭제할 질문",
            generated_at=datetime(2026, 7, 16, tzinfo=UTC),
        )
        FreeQuestionMessage.objects.create(
            external_ts="800.200",
            thread_ts=thread_ts,
            role=FreeQuestionMessage.Role.ASSISTANT,
            content="의미 없는 답변",
            generated_at=datetime(2026, 7, 16, 0, 1, tzinfo=UTC),
        )
        reconcile_slack_thread(thread_ts)
        item = KnowledgeItem.objects.get(source_key=f"slack:{thread_ts}:800.100")

        response = self.delete(f"/api/knowledge/{item.id}/")

        self.assertEqual(response.status_code, 204)
        item.refresh_from_db()
        self.assertIsNotNone(item.hidden_at)
        self.assertEqual(FreeQuestionMessage.objects.count(), 2)
        self.assertEqual(self.client.get("/api/free-question/").json()["count"], 0)
        self.assertEqual(self.client.get(f"/api/knowledge/{item.id}/").status_code, 404)
        self.assertEqual(self.client.get("/api/summary/").json()["knowledge"]["pending"], 0)
        self.assertEqual(eligible_pending_items(None, 10), [])

        reconcile_slack_thread(thread_ts)
        item.refresh_from_db()
        self.assertIsNotNone(item.hidden_at)

    def test_hides_cron_content_and_keeps_it_hidden_after_resync(self):
        job = CronJob.objects.create(
            external_id="hidden-cron",
            name="삭제할 Cron",
            category=CronJob.Category.OTHER,
        )
        run = ContentRun.objects.create(
            job=job,
            external_ts="900.100",
            status=ContentRun.Status.SUCCESS,
            title="삭제할 Cron 결과",
            body="의미 없는 결과",
            generated_at=datetime(2026, 7, 16, tzinfo=UTC),
        )
        reconcile_cron_runs([run.id])
        item = KnowledgeItem.objects.get(content_run=run)
        self.assertEqual(self.client.get("/api/search/?q=삭제할").json()["count"], 1)

        response = self.delete(f"/api/runs/{run.id}/")

        self.assertEqual(response.status_code, 204)
        run.refresh_from_db()
        item.refresh_from_db()
        self.assertIsNotNone(run.hidden_at)
        self.assertIsNotNone(item.hidden_at)
        self.assertTrue(ContentRun.objects.filter(pk=run.pk).exists())
        self.assertEqual(self.client.get("/api/runs/").json()["results"], [])
        self.assertEqual(self.client.get("/api/search/?q=삭제할").json()["count"], 0)
        self.assertEqual(self.client.get(f"/api/runs/{run.id}/").status_code, 404)
        self.client.get("/api/csrf/")
        self.assertEqual(
            self.client.patch(
                f"/api/runs/{run.id}/state/",
                data=json.dumps({"completed": True}),
                content_type="application/json",
                HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value,
            ).status_code,
            404,
        )

        run.body = "동기화된 수정 결과"
        run.save(update_fields=["body"])
        reconcile_cron_runs([run.id])
        item.refresh_from_db()
        self.assertIsNotNone(item.hidden_at)
