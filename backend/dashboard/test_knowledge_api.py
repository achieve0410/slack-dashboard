from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase

from .admin import ManualApprovalForm, approve_knowledge_items
from .models import (
    Category,
    ContentRun,
    CronJob,
    KnowledgeConsumptionState,
    KnowledgeItem,
)
from .services import reconcile_cron_runs

def create_category_path(path: str) -> Category:
    parent = None
    category = None
    accumulated = []
    for depth, name in enumerate(path.split("/"), start=1):
        accumulated.append(name)
        full_path = "/".join(accumulated)
        category, _ = Category.objects.get_or_create(
            path=full_path,
            defaults={"name": name, "parent": parent, "depth": depth, "is_active": True},
        )
        parent = category
    return category



def dt(hour: int) -> datetime:
    return datetime(2026, 7, 15, hour, tzinfo=UTC)


class KnowledgeApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.learning = create_category_path("학습")
        self.language = create_category_path("학습/언어")
        self.english = create_category_path("학습/언어/영어")
        self.japanese = create_category_path("학습/언어/일본어")
        self.life = create_category_path("생활")
        self.market = create_category_path("생활/금융/한국 주식")

        self.english_item = self.create_cron_item(
            "english-api",
            self.english,
            "영어 본문",
            1,
        )
        self.japanese_item = self.create_cron_item(
            "japanese-api",
            self.japanese,
            "일본어 본문",
            2,
        )
        self.market_item = self.create_cron_item(
            "market-api",
            self.market,
            "시장 본문",
            3,
        )
        self.pending_cron = self.create_cron_item(
            "pending-api",
            None,
            "미분류 본문",
            4,
        )
        self.classified_qa = self.create_slack_item(
            "classified",
            KnowledgeItem.Status.CLASSIFIED,
            5,
            answer="분류된 답변",
            category=self.english,
        )
        self.awaiting_qa = self.create_slack_item(
            "awaiting",
            KnowledgeItem.Status.AWAITING_ANSWER,
            6,
        )
        self.pending_qa = self.create_slack_item(
            "pending",
            KnowledgeItem.Status.PENDING,
            7,
            answer="분류 대기 답변",
        )
        self.review_qa = self.create_slack_item(
            "review",
            KnowledgeItem.Status.NEEDS_REVIEW,
            8,
            answer="검토 답변",
        )

    def create_cron_item(self, external_id, category, body, hour):
        job = CronJob.objects.create(
            external_id=external_id,
            name=external_id,
            category=CronJob.Category.OTHER,
        )
        run = ContentRun.objects.create(
            job=job,
            status=ContentRun.Status.SUCCESS,
            title=f"{external_id} title",
            body=body,
            generated_at=dt(hour),
        )
        reconcile_cron_runs([run.pk])
        if category is not None:
            item = KnowledgeItem.objects.get(content_run=run)
            item.category = category
            item.status = KnowledgeItem.Status.CLASSIFIED
            item.classification_model = "manual"
            item.classification_confidence = "1.000"
            item.classified_at = dt(hour)
            item.save()
        return KnowledgeItem.objects.get(content_run=run)

    def create_slack_item(self, suffix, status, hour, *, answer="", category=None):
        classified_at = dt(hour) if status == KnowledgeItem.Status.CLASSIFIED else None
        return KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key=f"slack:api:{suffix}",
            category=category,
            status=status,
            title=f"질문 {suffix}",
            summary=f"요약 {suffix}",
            question=f"질문 본문 {suffix}",
            answer=answer,
            source_hash=str(hour)[-1] * 64,
            generated_at=dt(hour),
            classification_model="test-model" if classified_at else "",
            classification_confidence="0.900" if classified_at else None,
            classification_reason="테스트 분류" if classified_at else "",
            classified_at=classified_at,
        )

    @staticmethod
    def find_category(nodes, path):
        for node in nodes:
            if node["path"] == path:
                return node
            found = KnowledgeApiTests.find_category(node["children"], path)
            if found:
                return found
        return None

    def test_categories_returns_nested_tree_with_descendant_counts(self):
        response = self.client.get("/api/categories/")

        self.assertEqual(response.status_code, 200)
        roots = response.json()["results"]
        self.assertEqual([node["path"] for node in roots], ["생활", "학습"])
        self.assertEqual(self.find_category(roots, "학습")["classified_count"], 3)
        self.assertEqual(
            self.find_category(roots, "학습/언어")["classified_count"], 3
        )
        english = self.find_category(roots, "학습/언어/영어")
        self.assertEqual(english["classified_count"], 2)
        self.assertEqual(english["parent_id"], self.language.pk)
        self.assertEqual(english["depth"], 3)
        self.assertEqual(self.find_category(roots, "생활")["classified_count"], 1)

    def test_inactive_parent_hides_descendants_from_tree_and_category_filter(self):
        self.learning.is_active = False
        self.learning.save(update_fields=["is_active"])

        roots = self.client.get("/api/categories/").json()["results"]
        response = self.client.get(f"/api/knowledge/?category={self.english.pk}")

        self.assertIsNone(self.find_category(roots, "학습"))
        self.assertIsNone(self.find_category(roots, "학습/언어/영어"))
        self.assertEqual(response.status_code, 404)

    def test_knowledge_lists_all_visible_items_with_deterministic_pagination(self):
        response = self.client.get("/api/knowledge/?limit=2&offset=1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 8)
        self.assertEqual(payload["next_offset"], 3)
        self.assertEqual(
            [item["id"] for item in payload["results"]],
            [self.pending_qa.pk, self.awaiting_qa.pk],
        )
        all_items = self.client.get("/api/knowledge/").json()["results"]
        self.assertEqual(all_items[0]["id"], self.review_qa.pk)
        self.assertEqual(
            all_items[0]["detail_url"],
            f"/knowledge/{self.review_qa.pk}",
        )
        cron_card = next(
            item for item in all_items if item["id"] == self.market_item.pk
        )
        self.assertEqual(
            cron_card["detail_url"], f"/runs/{self.market_item.content_run_id}"
        )
        self.assertIn(self.pending_cron.pk, [item["id"] for item in all_items])

    def test_knowledge_uses_primary_key_as_the_newest_tie_breaker(self):
        newer_id = self.create_slack_item(
            "same-time",
            KnowledgeItem.Status.CLASSIFIED,
            5,
            answer="동일 시각 답변",
            category=self.english,
        )

        response = self.client.get("/api/knowledge/?status=classified&limit=2")

        self.assertEqual(
            [item["id"] for item in response.json()["results"]],
            [newer_id.pk, self.classified_qa.pk],
        )

    def test_default_list_keeps_classified_items_in_inactive_categories(self):
        self.english.is_active = False
        self.english.save(update_fields=["is_active"])

        response = self.client.get("/api/knowledge/")
        inactive_filter = self.client.get(
            f"/api/knowledge/?category={self.english.pk}"
        )

        self.assertIn(
            self.classified_qa.pk,
            [item["id"] for item in response.json()["results"]],
        )
        self.assertEqual(inactive_filter.status_code, 404)

    def test_category_and_source_filters_use_exact_or_descendant_paths(self):
        learning = self.client.get(f"/api/knowledge/?category={self.learning.pk}")
        english = self.client.get(f"/api/knowledge/?category={self.english.pk}")
        slack = self.client.get("/api/knowledge/?source_type=slack_qa")

        self.assertEqual(
            {item["id"] for item in learning.json()["results"]},
            {self.english_item.pk, self.japanese_item.pk, self.classified_qa.pk},
        )
        self.assertEqual(
            {item["id"] for item in english.json()["results"]},
            {self.english_item.pk, self.classified_qa.pk},
        )
        self.assertEqual(
            [item["id"] for item in slack.json()["results"]],
            [
                self.review_qa.pk,
                self.pending_qa.pk,
                self.awaiting_qa.pk,
                self.classified_qa.pk,
            ],
        )

    def test_status_filter_separates_each_knowledge_workflow_state(self):
        expected = {
            KnowledgeItem.Status.CLASSIFIED: {
                self.english_item.pk,
                self.japanese_item.pk,
                self.market_item.pk,
                self.classified_qa.pk,
            },
            KnowledgeItem.Status.PENDING: {
                self.pending_cron.pk,
                self.pending_qa.pk,
            },
            KnowledgeItem.Status.AWAITING_ANSWER: {self.awaiting_qa.pk},
            KnowledgeItem.Status.NEEDS_REVIEW: {self.review_qa.pk},
        }

        for status, item_ids in expected.items():
            with self.subTest(status=status):
                response = self.client.get(f"/api/knowledge/?status={status}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    {item["id"] for item in response.json()["results"]},
                    item_ids,
                )

    def test_category_filter_keeps_accent_distinct_roots_separate(self):
        plain = Category.objects.create(name="Cafe", path="Cafe", depth=1)
        accented = Category.objects.create(name="Café", path="Café", depth=1)
        plain_item = self.create_slack_item(
            "plain-cafe",
            KnowledgeItem.Status.CLASSIFIED,
            9,
            answer="plain",
            category=plain,
        )
        accented_item = self.create_slack_item(
            "accented-cafe",
            KnowledgeItem.Status.CLASSIFIED,
            10,
            answer="accented",
            category=accented,
        )

        response = self.client.get(f"/api/knowledge/?category={plain.pk}")

        ids = {item["id"] for item in response.json()["results"]}
        self.assertIn(plain_item.pk, ids)
        self.assertNotIn(accented_item.pk, ids)

    def test_bookmark_filter_applies_only_to_linked_cron_run(self):
        KnowledgeConsumptionState.objects.create(
            knowledge_item=self.japanese_item,
            bookmarked_at=dt(9),
        )

        response = self.client.get("/api/knowledge/?bookmarked=1")

        self.assertEqual(
            [item["id"] for item in response.json()["results"]],
            [self.japanese_item.pk],
        )
        self.assertTrue(response.json()["results"][0]["state"]["bookmarked"])

    def test_knowledge_searches_server_side_and_supports_stable_date_sorting(self):
        search = self.client.get("/api/knowledge/?q=영어%20본문")
        oldest = self.client.get("/api/knowledge/?sort=oldest")

        self.assertEqual(search.status_code, 200)
        self.assertEqual(
            [item["id"] for item in search.json()["results"]],
            [self.english_item.pk],
        )
        self.assertEqual(
            [item["id"] for item in oldest.json()["results"]],
            [
                self.english_item.pk,
                self.japanese_item.pk,
                self.market_item.pk,
                self.pending_cron.pk,
                self.classified_qa.pk,
                self.awaiting_qa.pk,
                self.pending_qa.pk,
                self.review_qa.pk,
            ],
        )

    @patch("dashboard.views.timezone.now", return_value=datetime(2026, 7, 15, 10, tzinfo=UTC))
    def test_today_period_uses_local_date_and_matches_summary_kpi(self, _now):
        yesterday = self.create_slack_item(
            "yesterday",
            KnowledgeItem.Status.CLASSIFIED,
            9,
            answer="어제 답변",
            category=self.english,
        )
        yesterday.generated_at = datetime(2026, 7, 14, 14, 59, tzinfo=UTC)
        yesterday.save(update_fields=["generated_at"])
        tomorrow = self.create_slack_item(
            "tomorrow",
            KnowledgeItem.Status.CLASSIFIED,
            10,
            answer="내일 답변",
            category=self.english,
        )
        tomorrow.generated_at = datetime(2026, 7, 15, 15, tzinfo=UTC)
        tomorrow.save(update_fields=["generated_at"])

        response = self.client.get("/api/knowledge/?period=today")
        summary = self.client.get("/api/summary/").json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {item["id"] for item in response.json()["results"]},
            {
                self.english_item.pk,
                self.japanese_item.pk,
                self.market_item.pk,
                self.pending_cron.pk,
                self.classified_qa.pk,
                self.awaiting_qa.pk,
                self.pending_qa.pk,
                self.review_qa.pk,
            },
        )
        self.assertEqual(response.json()["count"], 8)
        self.assertEqual(summary["knowledge"]["generated_today"], 8)

    def test_global_search_includes_classified_and_inbox_content(self):
        classified = self.client.get("/api/search/?q=분류된%20답변")
        pending = self.client.get("/api/search/?q=미분류%20본문")

        self.assertEqual(
            [item["id"] for item in classified.json()["results"]],
            [self.classified_qa.pk],
        )
        self.assertEqual(
            [item["id"] for item in pending.json()["results"]],
            [self.pending_cron.pk],
        )

    def test_free_question_search_and_sort_are_applied_before_pagination(self):
        search = self.client.get("/api/free-question/?q=검토%20답변")
        oldest = self.client.get("/api/free-question/?sort=oldest")

        self.assertEqual(
            [item["id"] for item in search.json()["results"]],
            [self.review_qa.pk],
        )
        self.assertEqual(
            [item["id"] for item in oldest.json()["results"]],
            [self.awaiting_qa.pk, self.pending_qa.pk, self.review_qa.pk],
        )

    def test_detail_returns_qa_snapshot_and_cron_link_without_run_content(self):
        qa = self.client.get(f"/api/knowledge/{self.classified_qa.pk}/")
        cron = self.client.get(f"/api/knowledge/{self.english_item.pk}/")

        self.assertEqual(qa.status_code, 200)
        self.assertEqual(qa.json()["question"], "질문 본문 classified")
        self.assertEqual(qa.json()["answer"], "분류된 답변")
        self.assertEqual(qa.json()["category_path"], "학습/언어/영어")
        self.assertEqual(qa.json()["classification_model"], "test-model")
        self.assertEqual(cron.status_code, 200)
        self.assertEqual(
            cron.json()["content_run_id"],
            self.english_item.content_run_id,
        )
        self.assertFalse(cron.json()["state"]["read"])
        self.assertFalse(qa.json()["state"]["read"])
        for duplicated_field in (
            "body",
            "citations",
            "responses",
            "question",
            "answer",
        ):
            self.assertNotIn(duplicated_field, cron.json())

    def test_free_question_is_status_board_and_excludes_classified_items(self):
        response = self.client.get("/api/free-question/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            [item["id"] for item in payload["results"]],
            [self.review_qa.pk, self.pending_qa.pk, self.awaiting_qa.pk],
        )
        awaiting = payload["results"][-1]
        self.assertFalse(awaiting["has_answer"])
        self.assertEqual(awaiting["status_label"], "답변 대기")
        self.assertNotIn(
            self.classified_qa.pk,
            [item["id"] for item in payload["results"]],
        )

    def test_invalid_filters_and_missing_detail_return_client_errors(self):
        cases = (
            ("/api/knowledge/?category=bad", 400),
            ("/api/knowledge/?category=999999", 404),
            ("/api/knowledge/?source_type=bad", 400),
            ("/api/knowledge/?limit=0", 400),
            ("/api/knowledge/?limit=101", 400),
            ("/api/knowledge/?offset=-1", 400),
            ("/api/knowledge/?offset=bad", 400),
            ("/api/free-question/?limit=bad", 400),
            ("/api/knowledge/?sort=bad", 400),
            ("/api/knowledge/?period=week", 400),
            ("/api/knowledge/?status=bad", 400),
            ("/api/search/", 400),
            (f"/api/search/?q={'a' * 201}", 400),
            ("/api/knowledge/999999/", 404),
        )
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, expected)

    def test_manual_classification_assigns_pending_item(self):
        response = self.client.patch(
            f"/api/knowledge/{self.pending_qa.pk}/classification/",
            data={
                "category_id": self.english.pk,
                "review_note": "페이지에서 직접 분류",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.pending_qa.refresh_from_db()
        self.assertEqual(self.pending_qa.status, KnowledgeItem.Status.CLASSIFIED)
        self.assertEqual(self.pending_qa.category_id, self.english.pk)
        self.assertEqual(self.pending_qa.classification_model, "manual")
        self.assertEqual(
            self.pending_qa.classification_reason,
            "Manual review: 페이지에서 직접 분류",
        )
        self.assertIsNone(self.pending_qa.reviewed_by_id)
        self.assertEqual(response.json()["category_path"], "학습/언어/영어")

    def test_manual_classification_rejects_awaiting_answer_and_invalid_category(self):
        awaiting = self.client.patch(
            f"/api/knowledge/{self.awaiting_qa.pk}/classification/",
            data={
                "category_id": self.english.pk,
                "review_note": "답변 전 분류",
            },
            content_type="application/json",
        )
        invalid = self.client.patch(
            f"/api/knowledge/{self.review_qa.pk}/classification/",
            data={"category_id": "", "review_note": "직접 분류"},
            content_type="application/json",
        )

        self.assertEqual(awaiting.status_code, 409)
        self.assertEqual(invalid.status_code, 400)
        self.awaiting_qa.refresh_from_db()
        self.review_qa.refresh_from_db()
        self.assertEqual(self.awaiting_qa.status, KnowledgeItem.Status.AWAITING_ANSWER)
        self.assertEqual(self.review_qa.status, KnowledgeItem.Status.NEEDS_REVIEW)


class ManualApprovalTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="reviewer",
            password="unused",
        )
        self.english = create_category_path("학습/언어/영어")
        self.japanese = create_category_path("학습/언어/일본어")
        self.review_slack = self.create_slack(
            "review",
            KnowledgeItem.Status.NEEDS_REVIEW,
            "답변",
        )
        self.pending_slack = self.create_slack(
            "pending",
            KnowledgeItem.Status.PENDING,
            "답변",
        )
        self.awaiting_slack = self.create_slack(
            "awaiting",
            KnowledgeItem.Status.AWAITING_ANSWER,
            "",
        )
        self.classified_slack = self.create_slack(
            "classified",
            KnowledgeItem.Status.CLASSIFIED,
            "답변",
            self.english,
        )
        self.empty_cron = self.create_empty_cron()

    def create_slack(self, suffix, status, answer, category=None):
        return KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key=f"slack:admin:{suffix}",
            category=category,
            status=status,
            title=suffix,
            summary=suffix,
            question=f"질문 {suffix}",
            answer=answer,
            source_hash=suffix.ljust(64, "0"),
            generated_at=dt(1),
            classified_at=dt(1) if status == KnowledgeItem.Status.CLASSIFIED else None,
        )

    def create_empty_cron(self):
        job = CronJob.objects.create(
            external_id="empty-admin",
            name="empty-admin",
            category=CronJob.Category.OTHER,
        )
        run = ContentRun.objects.create(
            job=job,
            status=ContentRun.Status.SUCCESS,
            title="빈 Cron",
            body="",
            generated_at=dt(2),
        )
        reconcile_cron_runs([run.pk])
        return KnowledgeItem.objects.get(content_run=run)

    def test_approves_pending_and_review_items_reassigns_classified(self):
        self.classified_slack.classification_confidence = "0.875"
        self.classified_slack.save(update_fields=["classification_confidence"])
        item_ids = [
            self.review_slack.pk,
            self.pending_slack.pk,
            self.awaiting_slack.pk,
            self.classified_slack.pk,
            self.empty_cron.pk,
        ]

        updated, skipped = approve_knowledge_items(
            item_ids,
            self.japanese.pk,
            self.user,
            "검토 후 일본어로 확정",
        )

        self.assertEqual((updated, skipped), (4, 1))
        for item in (
            self.review_slack,
            self.pending_slack,
            self.classified_slack,
            self.empty_cron,
        ):
            item.refresh_from_db()
            self.assertEqual(item.category_id, self.japanese.pk)
            self.assertEqual(item.status, KnowledgeItem.Status.CLASSIFIED)
            self.assertIsNotNone(item.classified_at)
            self.assertEqual(item.reviewed_by_id, self.user.pk)
            self.assertIsNotNone(item.reviewed_at)
            self.assertEqual(item.classification_model, "manual")
            self.assertIsNone(item.classification_confidence)
            self.assertEqual(
                item.classification_reason,
                "Manual review: 검토 후 일본어로 확정",
            )
            item.full_clean()
        self.awaiting_slack.refresh_from_db()
        self.assertEqual(
            self.awaiting_slack.status,
            KnowledgeItem.Status.AWAITING_ANSWER,
        )

    def test_inactive_category_rejects_without_mutating_items(self):
        self.japanese.is_active = False
        self.japanese.save(update_fields=["is_active"])

        with self.assertRaises(ValidationError):
            approve_knowledge_items(
                [self.review_slack.pk],
                self.japanese.pk,
                self.user,
                "비활성 카테고리",
            )

        self.review_slack.refresh_from_db()
        self.assertEqual(self.review_slack.status, KnowledgeItem.Status.NEEDS_REVIEW)
        self.assertIsNone(self.review_slack.category_id)

    def test_inactive_ancestor_excludes_admin_choice_and_rejects_approval(self):
        learning = create_category_path("학습")
        learning.is_active = False
        learning.save(update_fields=["is_active"])

        form = ManualApprovalForm()
        self.assertNotIn(
            self.english.pk,
            form.fields["category"].queryset.values_list("pk", flat=True),
        )
        with self.assertRaises(ValidationError):
            approve_knowledge_items(
                [self.review_slack.pk],
                self.english.pk,
                self.user,
                "비활성 상위 카테고리",
            )

    def test_seeded_cron_manual_override_survives_source_refresh(self):
        job = CronJob.objects.create(
            external_id="manual-seeded",
            name="manual-seeded",
            category=CronJob.Category.OTHER,
        )
        run = ContentRun.objects.create(
            job=job,
            status=ContentRun.Status.SUCCESS,
            title="원래 제목",
            body="원래 본문",
            generated_at=dt(3),
        )
        reconcile_cron_runs([run.pk])
        item = KnowledgeItem.objects.get(content_run=run)
        approve_knowledge_items(
            [item.pk],
            self.japanese.pk,
            self.user,
            "시드 분류 수동 교정",
        )

        run.title = "갱신 제목"
        run.body = "갱신 본문"
        run.save(update_fields=["title", "body"])
        stats = reconcile_cron_runs([run.pk])
        item.refresh_from_db()

        self.assertEqual(stats["manual_overrides"], 1)
        self.assertEqual(item.category_id, self.japanese.pk)
        self.assertEqual(item.classification_model, "manual")
        self.assertEqual(item.reviewed_by_id, self.user.pk)
        self.assertIsNotNone(item.reviewed_at)
        self.assertEqual(item.title, "갱신 제목")
        self.assertEqual(item.summary, "갱신 본문")

    def test_validation_failure_rolls_back_the_whole_batch(self):
        second = self.create_slack(
            "review-2",
            KnowledgeItem.Status.NEEDS_REVIEW,
            "답변",
        )

        def validate(item, *args, **kwargs):
            if item.pk == second.pk:
                raise ValidationError("강제 검증 실패")

        with patch.object(
            KnowledgeItem,
            "full_clean",
            autospec=True,
            side_effect=validate,
        ):
            with self.assertRaises(ValidationError):
                approve_knowledge_items(
                    [self.review_slack.pk, second.pk],
                    self.english.pk,
                    self.user,
                    "원자성 검증",
                )

        self.review_slack.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.review_slack.status, KnowledgeItem.Status.NEEDS_REVIEW)
        self.assertEqual(second.status, KnowledgeItem.Status.NEEDS_REVIEW)
        self.assertIsNone(self.review_slack.reviewed_by_id)
        self.assertIsNone(second.reviewed_by_id)

    def test_admin_action_renders_confirmation_and_applies_selection(self):
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save(update_fields=["is_staff", "is_superuser"])
        self.client.force_login(self.user)
        action_data = {
            "action": "approve_classification",
            "_selected_action": [str(self.review_slack.pk)],
            "select_across": "0",
            "index": "0",
        }

        confirmation = self.client.post(
            "/admin/dashboard/knowledgeitem/",
            action_data,
        )

        self.assertEqual(confirmation.status_code, 200)
        self.assertTemplateUsed(
            confirmation,
            "admin/dashboard/knowledgeitem/approve_classification.html",
        )

        applied = self.client.post(
            "/admin/dashboard/knowledgeitem/",
            {
                **action_data,
                "apply": "1",
                "category": str(self.english.pk),
                "review_note": "관리자 화면 검증",
            },
        )

        self.assertEqual(applied.status_code, 302)
        self.review_slack.refresh_from_db()
        self.assertEqual(self.review_slack.status, KnowledgeItem.Status.CLASSIFIED)
        self.assertEqual(self.review_slack.category_id, self.english.pk)
        self.assertEqual(self.review_slack.reviewed_by_id, self.user.pk)
