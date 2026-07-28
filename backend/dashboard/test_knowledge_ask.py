import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from . import llm
from .models import (
    Category,
    ContentRun,
    CronJob,
    KnowledgeAsk,
    KnowledgeAskSource,
    KnowledgeItem,
)
from .services import reconcile_cron_runs


NOW = datetime(2026, 7, 28, 2, 0, tzinfo=UTC)
CONFIG = llm.LLMConfig(
    provider="anthropic",
    model="test-model",
    api_key="test-key",
    max_tokens=1000,
)


def create_classified_item(title: str, body: str) -> KnowledgeItem:
    parent, _ = Category.objects.get_or_create(
        path="운영",
        defaults={"name": "운영", "depth": 1},
    )
    category, _ = Category.objects.get_or_create(
        path="운영/배포",
        defaults={
            "name": "배포",
            "parent": parent,
            "depth": 2,
        },
    )
    job = CronJob.objects.create(
        external_id=f"ask:{CronJob.objects.count()}",
        name=title,
        channel_id="C1",
    )
    run = ContentRun.objects.create(
        job=job,
        external_ts=f"{job.pk}.200",
        status=ContentRun.Status.SUCCESS,
        title=title,
        body=body,
        generated_at=NOW,
    )
    reconcile_cron_runs([run.pk])
    item = KnowledgeItem.objects.get(content_run=run)
    item.status = KnowledgeItem.Status.CLASSIFIED
    item.category = category
    item.classified_at = NOW
    item.save()
    return item


class KnowledgeAskApiTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="owner",
            password="test-password",
        )
        self.client.force_login(user)
        self.item = create_classified_item(
            "배포 승인 절차",
            "운영 배포 전에는 담당자의 승인을 받고 체크리스트를 확인합니다.",
        )

    def ask_response(self, payload: dict):
        return llm.LLMResponse(
            text=json.dumps(payload, ensure_ascii=False),
            usage={
                "model": "test-model",
                "provider": "anthropic",
                "input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 30,
                "api_calls": 1,
            },
        )

    def test_returns_only_validated_cited_sources(self):
        response_payload = {
            "answer": "배포 전 담당자 승인과 체크리스트 확인이 필요합니다.",
            "source_ids": [self.item.pk],
            "insufficient_evidence": False,
        }
        with patch(
            "dashboard.knowledge_ask.llm.resolve_llm_config",
            return_value=CONFIG,
        ), patch(
            "dashboard.knowledge_ask.llm.preflight_llm",
        ), patch(
            "dashboard.knowledge_ask.llm.complete",
            return_value=self.ask_response(response_payload),
        ) as complete:
            response = self.client.post(
                "/api/ask/",
                data={"question": "배포 전에 무엇을 확인해야 하나요?", "locale": "ko"},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["insufficient_evidence"])
        self.assertEqual(
            response.json()["sources"][0]["knowledge_item_id"],
            self.item.pk,
        )
        self.assertEqual(KnowledgeAsk.objects.count(), 1)
        self.assertEqual(KnowledgeAskSource.objects.get().knowledge_item, self.item)
        self.assertEqual(complete.call_args.kwargs["operation"], "ask")

    def test_rejects_source_id_outside_candidate_set(self):
        with patch(
            "dashboard.knowledge_ask.llm.resolve_llm_config",
            return_value=CONFIG,
        ), patch(
            "dashboard.knowledge_ask.llm.preflight_llm",
        ), patch(
            "dashboard.knowledge_ask.llm.complete",
            return_value=self.ask_response(
                {
                    "answer": "근거 없는 답변",
                    "source_ids": [999999],
                    "insufficient_evidence": False,
                }
            ),
        ):
            response = self.client.post(
                "/api/ask/",
                data={"question": "배포 절차는?"},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["code"], "invalid_llm_response")
        self.assertFalse(KnowledgeAsk.objects.exists())

    def test_empty_corpus_returns_insufficient_without_llm(self):
        KnowledgeItem.objects.all().delete()
        with patch("dashboard.knowledge_ask.llm.complete") as complete:
            response = self.client.post(
                "/api/ask/",
                data={"question": "없는 정보는?", "locale": "ko"},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["insufficient_evidence"])
        self.assertEqual(response.json()["sources"], [])
        complete.assert_not_called()

    def test_verified_filter_excludes_overdue_items(self):
        self.item.verification_status = KnowledgeItem.VerificationStatus.VERIFIED
        self.item.verified_at = timezone.now() - timedelta(days=100)
        self.item.review_due_at = timezone.now() - timedelta(days=1)
        self.item.save()

        with patch("dashboard.knowledge_ask.llm.complete") as complete:
            response = self.client.post(
                "/api/ask/",
                data={
                    "question": "배포 절차는?",
                    "filters": {"verification": "verified"},
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["insufficient_evidence"])
        complete.assert_not_called()

    def test_feedback_and_history_are_persisted(self):
        ask = KnowledgeAsk.objects.create(
            question="질문",
            answer="답변",
            insufficient_evidence=False,
        )
        feedback = self.client.patch(
            f"/api/ask/{ask.pk}/feedback/",
            data={"feedback": "unhelpful", "note": "출처가 부족함"},
            content_type="application/json",
        )
        history = self.client.get("/api/ask/?limit=10")

        self.assertEqual(feedback.status_code, 200)
        self.assertEqual(feedback.json()["feedback"], "unhelpful")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["results"][0]["id"], ask.pk)
