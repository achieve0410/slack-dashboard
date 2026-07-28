from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .models import (
    CronJob,
    KnowledgeItem,
    QuizQuestion,
    ScheduleEvent,
)
from .onboarding import DEMO_JOB_ID, DEMO_SCHEDULE_PREFIX


class OnboardingTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="owner",
            password="test-password",
        )
        self.client.force_login(user)

    def test_empty_status_exposes_guided_steps_without_secrets(self):
        response = self.client.get("/api/onboarding/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["empty"])
        self.assertEqual(response.json()["total_steps"], 6)
        self.assertNotIn("token", response.json()["configuration"])

    def test_demo_seed_is_idempotent_and_purge_removes_only_demo_records(self):
        first = self.client.post("/api/onboarding/")
        second = self.client.post("/api/onboarding/")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            KnowledgeItem.objects.filter(
                content_run__job__external_id=DEMO_JOB_ID
            ).count(),
            3,
        )
        self.assertEqual(
            QuizQuestion.objects.filter(
                knowledge_item__content_run__job__external_id=DEMO_JOB_ID
            ).count(),
            10,
        )
        self.assertEqual(
            ScheduleEvent.objects.filter(
                source_hash__startswith=DEMO_SCHEDULE_PREFIX
            ).count(),
            2,
        )
        self.assertTrue(second.json()["onboarding"]["demo_loaded"])

        purged = self.client.delete("/api/onboarding/")

        self.assertEqual(purged.status_code, 200)
        self.assertFalse(CronJob.objects.filter(external_id=DEMO_JOB_ID).exists())
        self.assertFalse(
            ScheduleEvent.objects.filter(
                source_hash__startswith=DEMO_SCHEDULE_PREFIX
            ).exists()
        )
        self.assertTrue(purged.json()["onboarding"]["empty"])

    def test_management_command_uses_same_demo_lifecycle(self):
        call_command("seed_demo_data")
        self.assertTrue(CronJob.objects.filter(external_id=DEMO_JOB_ID).exists())

        call_command("seed_demo_data", purge=True)
        self.assertFalse(CronJob.objects.filter(external_id=DEMO_JOB_ID).exists())

    def test_overdue_verification_does_not_complete_onboarding_step(self):
        call_command("seed_demo_data")
        KnowledgeItem.objects.filter(
            content_run__job__external_id=DEMO_JOB_ID
        ).update(review_due_at=timezone.now() - timedelta(days=1))

        status = self.client.get("/api/onboarding/").json()

        verify_step = next(
            step for step in status["steps"] if step["key"] == "verify"
        )
        self.assertFalse(verify_step["complete"])
