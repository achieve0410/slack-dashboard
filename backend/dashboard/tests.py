import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase

from dashboard.management.commands.sync_slack import Command

from .admin import CategoryAdmin
from .models import (
    Category,
    Citation,
    ContentRun,
    CronJob,
    FreeQuestionMessage,
    KnowledgeConsumptionState,
    KnowledgeItem,
    UserRunState,
)
from .services import (
    extract_citations,
    parse_slack_response,
    reconcile_cron_runs,
    reconcile_slack_thread,
    source_hash,
    summarize,
)


def create_category_path(path: str) -> Category:
    """Create a full Category chain for `path` (e.g. "학습/언어/영어").

    Category.save() derives path_key/identity_hash from path automatically,
    so callers only need to supply name/path/parent/depth.
    """
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


class FakeSlackClient:
    """Minimal stand-in for slack_sdk.WebClient covering the two methods
    sync_slack.Command.sync_knowledge_channel relies on."""

    def __init__(self, messages, *, channel_name="general"):
        self.messages = messages
        self.channel_name = channel_name

    def conversations_history(self, channel, limit=200, cursor=None):
        return {"messages": self.messages}

    def conversations_info(self, channel):
        return {"channel": {"name": self.channel_name}}


class SlackParserTests(TestCase):
    def test_parses_successful_cron_message(self):
        parsed = parse_slack_response(
            "Cronjob Response: 영어 학습 (job_id: abc123)\n-------------\n*오늘의 학습*"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.job_id, "abc123")
        self.assertEqual(parsed.status, "success")
        self.assertEqual(parsed.body, "*오늘의 학습*")

    def test_parses_failed_cron_message(self):
        parsed = parse_slack_response(
            "Cronjob Response: AWS 학습 (job_id: aws1)\n-------------\n:warning: Cron failed: timeout"
        )
        self.assertEqual(parsed.status, "failed")
        self.assertIn("timeout", parsed.error)

    def test_extracts_unique_slack_links(self):
        citations = extract_citations(
            "출처 <https://example.com/a|Example> 및 <https://example.com/a|Duplicate>"
        )
        self.assertEqual(citations, [{"url": "https://example.com/a", "title": "Example"}])

    def test_summarize_preserves_markdown_boundaries(self):
        content = """# 인천(ICN) ↔ 삿포로 신치토세(CTS) 항공권 확인

**여행 조건:** 2026년 12월 23일(수) 출국, 12월 27일(일) 귀국 / 성인 1명 / 이코노미 / 출국 오전, 귀국 점심~오후 선호

## 추천 왕복 조합

|구간|항공사|실제 출발·도착 시각|직항 여부|
|---|---|---|---|
|출국 12/23(수)|제주항공|**ICN 07:15 → CTS 10:00**|직항, 2시간 45분|
|귀국 12/27(일)|제주항공|**CTS 16:00 → ICN 19:25**|직항, 3시간 25분|

- 위 조합은 요청한 **출국 오전 / 귀국 오후** 조건을 모두 충족합니다.
- 같은 날짜·노선의 왕복 검색에서 제주항공 직항 출국편은 **성인 1인 왕복 ₩806,100부터**로 표시됐습니다. 따라서 이 조합은 **약 80.6만 원부터**, 위탁수하물·좌석지정 등을 더하면 **85만~95만 원** 예산을 권합니다.
- 항공권 가격은 실시간 변동되므로, **₩806,100은 조회 시점의 시작가**입니다. 특히 귀국 16:00편을 선택하는 최종 예약 화면에서 재확인해야 합니다.

## 12/27(일) 점심~오후 직항 귀국편 비교

|우선순위|항공사|CTS 출발 → ICN 도착|비고|
|---:|---|---|---|
|1|진에어|**12:25 → 15:40**|점심 직후 출발, 직항 3시간 15분|
"""
        summary = summarize(content)
        self.assertLessEqual(len(summary), 600)
        self.assertIn("|귀국 12/27(일)|제주항공|", summary)
        self.assertNotIn("|우선순위|항공사|CTS 출발", summary)
        self.assertNotIn("## 12/27(일)", summary)


class FreeQuestionSyncTests(TestCase):
    def test_selects_only_qualified_top_level_questions(self):
        messages = [
            {"ts": "100.100", "user": "U123", "text": "<@B123> 기준 이전"},
            {"ts": "100.300", "user": "U999", "text": "<@B123> 다른 사용자"},
            {"ts": "100.400", "user": "U123", "text": "멘션 없음"},
            {
                "ts": "100.500",
                "thread_ts": "100.200",
                "user": "U123",
                "text": "<@B123> 스레드 답글",
            },
            {
                "ts": "100.600",
                "thread_ts": "100.600",
                "user": "U123",
                "text": "<@B123> 답글이 생긴 새 질문",
            },
        ]

        selected = Command.select_free_question_roots(
            messages,
            bot_user_id="B123",
            user_id="U123",
            start_ts="100.200",
        )

        self.assertEqual([message["ts"] for message in selected], ["100.600"])

    def test_imports_and_updates_thread_messages(self):
        messages = [
            {"ts": "100.100", "user": "U123", "text": "<@B123> 자유 질문입니다."},
            {"ts": "100.200", "user": "B123", "text": "자유 답변입니다."},
        ]

        imported = Command.import_free_question_messages(messages, "B123", "100.100")

        self.assertEqual(imported, 2)
        self.assertEqual(FreeQuestionMessage.objects.count(), 2)
        question = FreeQuestionMessage.objects.get(external_ts="100.100")
        self.assertEqual(question.role, FreeQuestionMessage.Role.USER)
        self.assertEqual(question.content, "자유 질문입니다.")

        messages[1]["text"] = "수정된 답변입니다."
        Command.import_free_question_messages(messages, "B123", "100.100")
        self.assertEqual(FreeQuestionMessage.objects.count(), 2)
        self.assertEqual(
            FreeQuestionMessage.objects.get(external_ts="100.200").content,
            "수정된 답변입니다.",
        )
        item = KnowledgeItem.objects.get(source_key="slack:100.100:100.100")
        self.assertEqual(item.answer, "수정된 답변입니다.")
        self.assertEqual(item.status, KnowledgeItem.Status.PENDING)
        self.assertEqual(KnowledgeItem.objects.count(), 1)


class SyncRegressionTests(TestCase):
    def test_replaces_only_the_updated_runs_citations(self):
        unrelated_job = CronJob.objects.create(external_id="channel:C2", name="other")
        unrelated_run = ContentRun.objects.create(
            job=unrelated_job,
            external_ts="99.999",
            status=ContentRun.Status.SUCCESS,
            title="기존 실행",
            body="기존 본문",
            generated_at=datetime(2026, 7, 15, tzinfo=UTC),
        )
        Citation.objects.create(
            run=unrelated_run,
            title="Unrelated",
            url="https://example.com/unrelated",
        )

        command = Command()
        client = FakeSlackClient(
            [{"ts": "100.100", "text": "본문 <https://example.com/old|Old>"}]
        )
        command.sync_knowledge_channel(client, "C1", "B123")

        client.messages[0]["text"] = "본문 <https://example.com/new|New>"
        command.sync_knowledge_channel(client, "C1", "B123")

        updated_run = ContentRun.objects.get(external_ts="100.100")
        self.assertEqual(
            list(updated_run.citations.values_list("url", flat=True)),
            ["https://example.com/new"],
        )
        self.assertEqual(
            list(unrelated_run.citations.values_list("url", flat=True)),
            ["https://example.com/unrelated"],
        )
        self.assertEqual(KnowledgeItem.objects.count(), 1)
        self.assertEqual(
            updated_run.knowledge_item.source_hash,
            source_hash(updated_run.title, updated_run.body),
        )


class CronReconciliationTests(TestCase):
    def create_run(
        self,
        external_id: str,
        *,
        body: str,
        status: str = ContentRun.Status.SUCCESS,
        external_ts: str | None = None,
    ) -> ContentRun:
        job = CronJob.objects.create(
            external_id=external_id,
            name=external_id,
            category=CronJob.Category.OTHER,
        )
        return ContentRun.objects.create(
            job=job,
            external_ts=external_ts,
            status=status,
            title=f"{external_id} title",
            body=body,
            generated_at=datetime(2026, 7, 15, tzinfo=UTC),
        )

    def test_reconciles_successful_runs_and_is_idempotent(self):
        first_run = self.create_run("first", body="first body", external_ts=None)
        other = self.create_run("other", body="other body")
        empty = self.create_run("empty", body="   ")
        failed = self.create_run("failed", body="", status=ContentRun.Status.FAILED)
        Citation.objects.create(
            run=first_run,
            title="Source",
            url="https://example.com/source",
        )

        first = reconcile_cron_runs([first_run.pk, other.pk, empty.pk, failed.pk])
        snapshot = list(
            KnowledgeItem.objects.order_by("source_key").values_list(
                "source_key", "source_hash", "status", "category_id"
            )
        )
        second = reconcile_cron_runs([first_run.pk, other.pk, empty.pk, failed.pk])

        self.assertEqual(first["created"], 3)
        self.assertEqual(KnowledgeItem.objects.count(), 3)
        self.assertEqual(
            list(
                KnowledgeItem.objects.order_by("source_key").values_list(
                    "source_key", "source_hash", "status", "category_id"
                )
            ),
            snapshot,
        )
        self.assertEqual(second["unchanged"], 4)
        self.assertEqual(first_run.knowledge_item.source_key, f"cron:{first_run.pk}")
        self.assertIsNone(first_run.knowledge_item.category_id)
        self.assertEqual(first_run.knowledge_item.status, KnowledgeItem.Status.PENDING)
        self.assertEqual(first_run.knowledge_item.question, "")
        self.assertEqual(first_run.knowledge_item.answer, "")
        self.assertEqual(other.knowledge_item.status, KnowledgeItem.Status.PENDING)
        self.assertIsNone(other.knowledge_item.category_id)
        self.assertEqual(empty.knowledge_item.status, KnowledgeItem.Status.NEEDS_REVIEW)
        self.assertFalse(KnowledgeItem.objects.filter(content_run=failed).exists())
        self.assertEqual(Citation.objects.filter(run=first_run).count(), 1)

    def test_manual_override_persists_and_hash_change_resets_ai_mapping(self):
        known = self.create_run("known", body="old known")
        other = self.create_run("other", body="old other")
        reconcile_cron_runs([known.pk, other.pk])
        known_hash = known.knowledge_item.source_hash

        known_item = known.knowledge_item
        known_item.category = create_category_path("학습/언어/영어")
        known_item.status = KnowledgeItem.Status.CLASSIFIED
        known_item.classification_model = "manual"
        known_item.classification_confidence = "1.000"
        known_item.classification_reason = "manual decision"
        known_item.classified_at = datetime(2026, 7, 15, 1, tzinfo=UTC)
        known_item.reviewed_at = datetime(2026, 7, 15, 1, tzinfo=UTC)
        known_item.save()

        other_item = other.knowledge_item
        other_item.category = create_category_path("생활/금융/한국 주식")
        other_item.status = KnowledgeItem.Status.CLASSIFIED
        other_item.classification_model = "gpt-test"
        other_item.classification_confidence = "0.900"
        other_item.classification_reason = "AI decision"
        other_item.classified_at = datetime(2026, 7, 15, 1, tzinfo=UTC)
        other_item.save()

        reconcile_cron_runs([known.pk, other.pk])
        known_item.refresh_from_db()
        other_item.refresh_from_db()
        self.assertEqual(other_item.status, KnowledgeItem.Status.CLASSIFIED)
        self.assertEqual(other_item.classification_model, "gpt-test")

        known.body = "new known"
        known.save(update_fields=["body"])
        other.body = "new other"
        other.save(update_fields=["body"])
        stats = reconcile_cron_runs([known.pk, other.pk])

        known_item.refresh_from_db()
        other_item.refresh_from_db()
        self.assertNotEqual(known_item.source_hash, known_hash)
        self.assertEqual(known_item.status, KnowledgeItem.Status.CLASSIFIED)
        self.assertEqual(known_item.classification_model, "manual")
        self.assertEqual(other_item.status, KnowledgeItem.Status.PENDING)
        self.assertIsNone(other_item.category_id)
        self.assertEqual(other_item.classification_model, "")
        self.assertIsNone(other_item.classified_at)
        self.assertEqual(stats["source_resets"], 1)
        self.assertEqual(stats["manual_overrides"], 1)

    def test_removes_only_derived_item_when_run_becomes_failed(self):
        run = self.create_run("known", body="body")
        reconcile_cron_runs([run.pk])
        Citation.objects.create(run=run, url="https://example.com/source")

        run.status = ContentRun.Status.FAILED
        run.save(update_fields=["status"])
        stats = reconcile_cron_runs([run.pk])

        self.assertEqual(stats["deleted"], 1)
        self.assertFalse(KnowledgeItem.objects.filter(content_run=run).exists())
        self.assertTrue(ContentRun.objects.filter(pk=run.pk).exists())
        self.assertEqual(Citation.objects.filter(run=run).count(), 1)


class SlackReconciliationTests(TestCase):
    thread_ts = "500.000"

    def message(self, external_ts: str, role: str, content: str):
        return FreeQuestionMessage.objects.create(
            external_ts=external_ts,
            thread_ts=self.thread_ts,
            role=role,
            content=content,
            generated_at=datetime.fromtimestamp(float(external_ts), tz=UTC),
        )

    def test_aggregates_replies_handles_orphans_and_is_idempotent(self):
        orphan = self.message("499.900", FreeQuestionMessage.Role.ASSISTANT, "orphan")
        question = self.message("500.100", FreeQuestionMessage.Role.USER, "질문 1")
        answer_one = self.message("500.200", FreeQuestionMessage.Role.ASSISTANT, "답변 1")
        answer_two = self.message("500.300", FreeQuestionMessage.Role.ASSISTANT, "답변 2")
        waiting = self.message("500.400", FreeQuestionMessage.Role.USER, "질문 2")

        first = reconcile_slack_thread(self.thread_ts)
        item = KnowledgeItem.objects.get(source_key="slack:500.000:500.100")
        first_hashes = list(
            KnowledgeItem.objects.order_by("source_key").values_list(
                "source_key", "source_hash", "status"
            )
        )
        second = reconcile_slack_thread(self.thread_ts)

        self.assertEqual(first["created"], 1)
        self.assertEqual(first["orphan_messages"], 1)
        self.assertEqual(
            item.question,
            "## 초기 요청\n\n질문 1\n\n## 후속 요청 1\n\n질문 2",
        )
        self.assertEqual(item.answer, "")
        self.assertEqual(item.status, KnowledgeItem.Status.AWAITING_ANSWER)
        self.assertEqual(item.summary, "답변 대기 중")
        self.assertEqual(
            set(
                FreeQuestionMessage.objects.filter(knowledge_item=item).values_list(
                    "pk", flat=True
                )
            ),
            {question.pk, answer_one.pk, answer_two.pk, waiting.pk},
        )
        orphan.refresh_from_db()
        waiting.refresh_from_db()
        self.assertIsNone(orphan.knowledge_item_id)
        self.assertEqual(waiting.knowledge_item_id, item.pk)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(
            list(
                KnowledgeItem.objects.order_by("source_key").values_list(
                    "source_key", "source_hash", "status"
                )
            ),
            first_hashes,
        )

    def test_boundary_change_reassigns_stale_links_and_resets_classification(self):
        first_question = self.message(
            "500.100", FreeQuestionMessage.Role.USER, "첫 질문"
        )
        answer = self.message("500.300", FreeQuestionMessage.Role.ASSISTANT, "답변")
        reconcile_slack_thread(self.thread_ts)
        original_item = KnowledgeItem.objects.get(source_key="slack:500.000:500.100")
        original_item.category = create_category_path("학습/언어/영어")
        original_item.status = KnowledgeItem.Status.CLASSIFIED
        original_item.classification_model = "gpt-test"
        original_item.classification_confidence = "0.900"
        original_item.classified_at = datetime(2026, 7, 15, tzinfo=UTC)
        original_item.save()

        reconcile_slack_thread(self.thread_ts)
        original_item.refresh_from_db()
        self.assertEqual(original_item.status, KnowledgeItem.Status.CLASSIFIED)
        self.assertEqual(original_item.classification_model, "gpt-test")

        second_question = self.message(
            "500.200", FreeQuestionMessage.Role.USER, "둘째 질문"
        )
        reset_stats = reconcile_slack_thread(self.thread_ts)

        original_item.refresh_from_db()
        answer.refresh_from_db()
        self.assertEqual(original_item.status, KnowledgeItem.Status.PENDING)
        self.assertIsNone(original_item.category_id)
        self.assertEqual(original_item.classification_model, "")
        self.assertEqual(answer.knowledge_item_id, original_item.pk)
        self.assertEqual(original_item.answer, "답변")
        self.assertEqual(
            original_item.question,
            "## 초기 요청\n\n첫 질문\n\n## 후속 요청 1\n\n둘째 질문",
        )
        self.assertEqual(KnowledgeItem.objects.count(), 1)

        first_question.role = FreeQuestionMessage.Role.ASSISTANT
        first_question.save(update_fields=["role"])
        stats = reconcile_slack_thread(self.thread_ts)
        first_question.refresh_from_db()
        second_question.refresh_from_db()
        second_item = KnowledgeItem.objects.get(source_key="slack:500.000:500.200")

        self.assertGreaterEqual(stats["deleted"], 1)
        self.assertFalse(
            KnowledgeItem.objects.filter(source_key="slack:500.000:500.100").exists()
        )
        self.assertIsNone(first_question.knowledge_item_id)
        self.assertEqual(second_question.knowledge_item_id, second_item.pk)
        self.assertEqual(FreeQuestionMessage.objects.count(), 3)
        self.assertGreaterEqual(reset_stats["source_resets"], 1)

    def test_merges_root_request_and_followups_into_one_knowledge_item(self):
        root = self.message(
            "500.100", FreeQuestionMessage.Role.USER, "초기 요청"
        )
        first_answer = self.message(
            "500.200", FreeQuestionMessage.Role.ASSISTANT, "초기 답변"
        )
        followup = self.message(
            "500.300", FreeQuestionMessage.Role.USER, "빠뜨린 조건 보완"
        )
        revised_answer = self.message(
            "500.400", FreeQuestionMessage.Role.ASSISTANT, "보완된 최종 답변"
        )

        stats = reconcile_slack_thread(self.thread_ts)

        self.assertEqual(stats["created"], 1)
        self.assertEqual(
            KnowledgeItem.objects.filter(
                source_type=KnowledgeItem.SourceType.SLACK_QA,
                source_key__startswith=f"slack:{self.thread_ts}:",
            ).count(),
            1,
        )
        item = KnowledgeItem.objects.get(
            source_key=f"slack:{self.thread_ts}:500.100"
        )
        self.assertEqual(
            item.question,
            "## 초기 요청\n\n초기 요청\n\n## 후속 요청 1\n\n빠뜨린 조건 보완",
        )
        self.assertEqual(item.answer, "보완된 최종 답변")
        self.assertEqual(
            set(
                FreeQuestionMessage.objects.filter(knowledge_item=item).values_list(
                    "pk", flat=True
                )
            ),
            {root.pk, first_answer.pk, followup.pk, revised_answer.pk},
        )


class CategoryModelTests(TestCase):
    def test_seeded_category_tree_has_valid_depths_and_paths(self):
        leaf = create_category_path("학습/언어/영어")

        leaf.full_clean()

        self.assertEqual(leaf.depth, 3)
        self.assertEqual(leaf.parent.path, "학습/언어")
        self.assertEqual(leaf.path_key, "학습/언어/영어")
        self.assertEqual(leaf.identity_hash, Category.identity_digest(leaf.path_key))

    def test_rejects_invalid_depth_and_path(self):
        parent = create_category_path("학습/언어/영어")
        category = Category(
            name="문법",
            path="학습/언어/영어/문법",
            path_key="학습/언어/영어/문법",
            parent=parent,
            depth=4,
        )

        with self.assertRaises(ValidationError):
            category.full_clean()

        with self.assertRaises(IntegrityError), transaction.atomic():
            Category.objects.create(
                name="잘못된 깊이",
                path="잘못된 깊이",
                path_key="invalid-depth",
                depth=4,
            )

    def test_rejects_duplicate_canonical_path_and_protects_parent(self):
        Category.objects.create(
            name="English",
            path="English",
            depth=1,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Category.objects.create(
                name="english",
                path="english",
                depth=1,
            )

        create_category_path("학습/언어/영어")
        with self.assertRaises(ProtectedError):
            create_category_path("학습").delete()

    def test_exact_identity_keeps_accent_width_and_kana_variants_distinct(self):
        values = ("Cafe", "Café", "AI", "ＡＩ", "かな", "カナ")
        categories = [
            Category.objects.create(name=value, path=value, depth=1)
            for value in values
        ]

        self.assertEqual(len({category.identity_hash for category in categories}), 6)
        for category in categories:
            self.assertEqual(
                Category.exact_category(category.path_key).pk,
                category.pk,
            )

    def test_rejects_invalid_segments_and_self_parenting(self):
        invalid_segment = Category(
            name="잘못된/이름",
            path="잘못된/이름",
            path_key="잘못된/이름",
            depth=1,
        )
        with self.assertRaises(ValidationError) as segment_context:
            invalid_segment.full_clean()
        self.assertIn("name", segment_context.exception.message_dict)

        category = create_category_path("학습")
        category.parent = category
        category.path = "학습/학습"
        category.path_key = "학습/학습"
        category.depth = 2
        with self.assertRaises(ValidationError) as parent_context:
            category.full_clean()
        self.assertIn("parent", parent_context.exception.message_dict)

    def test_rejects_casefold_expanded_path_key_over_model_limit(self):
        root_name = "ß" * 100
        root = Category.objects.create(name=root_name, path=root_name, depth=1)
        child = Category(
            name=root_name,
            path=f"{root.path}/{root_name}",
            parent=root,
            depth=2,
        )

        with self.assertRaises(ValidationError) as context:
            child.full_clean()

        self.assertIn("path_key", context.exception.message_dict)

    def test_admin_keeps_existing_category_structure_read_only(self):
        category = create_category_path("학습/언어/영어")
        readonly = CategoryAdmin(Category, AdminSite()).get_readonly_fields(
            request=None,
            obj=category,
        )

        self.assertTrue(
            {"name", "path", "path_key", "parent", "depth"}.issubset(readonly)
        )

    def test_admin_deactivation_rejects_active_descendants_and_classified_items(self):
        category_admin = CategoryAdmin(Category, AdminSite())
        form = SimpleNamespace(changed_data=["is_active"])
        english = create_category_path("학습/언어/영어")
        learning = english.parent.parent
        learning.is_active = False
        with self.assertRaises(ValidationError):
            category_admin.save_model(None, learning, form, change=True)

        KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key="slack:admin:deactivation",
            category=english,
            status=KnowledgeItem.Status.CLASSIFIED,
            title="classified",
            summary="classified",
            question="question",
            answer="answer",
            source_hash="d" * 64,
            generated_at=datetime(2026, 7, 15, tzinfo=UTC),
            classified_at=datetime(2026, 7, 15, tzinfo=UTC),
        )
        english.is_active = False
        with self.assertRaises(ValidationError):
            category_admin.save_model(None, english, form, change=True)

    def test_admin_deactivation_allows_unused_leaf(self):
        category = create_category_path("학습/자격증/AWS")
        category.is_active = False

        CategoryAdmin(Category, AdminSite()).save_model(
            None,
            category,
            SimpleNamespace(changed_data=["is_active"]),
            change=True,
        )

        category.refresh_from_db()
        self.assertFalse(category.is_active)


class KnowledgeItemModelTests(TestCase):
    def setUp(self):
        self.job = CronJob.objects.create(
            external_id="knowledge-job",
            name="영어 학습",
            category=CronJob.Category.OTHER,
        )
        self.run = ContentRun.objects.create(
            job=self.job,
            external_ts=None,
            status=ContentRun.Status.SUCCESS,
            title="영어 학습",
            body="본문",
            generated_at=datetime(2026, 7, 15, tzinfo=UTC),
        )

    def test_accepts_questionless_cron_metadata_item(self):
        category = create_category_path("학습/언어/영어")
        item = KnowledgeItem(
            source_type=KnowledgeItem.SourceType.CRON,
            source_key=f"cron:{self.run.pk}",
            content_run=self.run,
            category=category,
            status=KnowledgeItem.Status.CLASSIFIED,
            title=self.run.title,
            summary="본문",
            question="",
            answer="",
            source_hash=hashlib.sha256(b"English\0body").hexdigest(),
            generated_at=self.run.generated_at,
            classified_at=self.run.generated_at,
        )

        item.full_clean()

    def test_rejects_cron_content_duplication(self):
        item = KnowledgeItem(
            source_type=KnowledgeItem.SourceType.CRON,
            source_key=f"cron:{self.run.pk}",
            content_run=self.run,
            status=KnowledgeItem.Status.PENDING,
            title=self.run.title,
            question="복제된 질문",
            answer="",
            source_hash="0" * 64,
            generated_at=self.run.generated_at,
        )

        with self.assertRaises(ValidationError):
            item.full_clean()

    def test_rejects_failed_run_and_non_pk_cron_source_key(self):
        self.run.status = ContentRun.Status.FAILED
        self.run.save(update_fields=["status"])
        item = KnowledgeItem(
            source_type=KnowledgeItem.SourceType.CRON,
            source_key="cron:external-timestamp",
            content_run=self.run,
            status=KnowledgeItem.Status.PENDING,
            title=self.run.title,
            question="",
            answer="",
            source_hash="0" * 64,
            generated_at=self.run.generated_at,
        )

        with self.assertRaises(ValidationError) as context:
            item.full_clean()

        self.assertIn("content_run", context.exception.message_dict)
        self.assertIn("source_key", context.exception.message_dict)

    def test_slack_answer_requirements_follow_item_status(self):
        awaiting = KnowledgeItem(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key="slack:100.000:100.100",
            status=KnowledgeItem.Status.AWAITING_ANSWER,
            title="질문",
            question="질문 본문",
            answer="",
            source_hash="0" * 64,
            generated_at=self.run.generated_at,
        )
        awaiting.full_clean()

        awaiting.status = KnowledgeItem.Status.PENDING
        with self.assertRaises(ValidationError) as context:
            awaiting.full_clean()

        self.assertIn("answer", context.exception.message_dict)


class ApiTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.job = CronJob.objects.create(
            external_id="job1",
            name="영어 학습",
            category=CronJob.Category.OTHER,
            last_status="success",
        )
        self.run = ContentRun.objects.create(
            job=self.job,
            external_ts="123.456",
            status=ContentRun.Status.SUCCESS,
            title="영어 학습",
            body="오늘의 문제",
            generated_at=datetime(2026, 7, 15, tzinfo=UTC),
        )
        Citation.objects.create(run=self.run, title="Source", url="https://example.com")

    def csrf_token(self) -> str:
        self.client.get("/api/csrf/")
        return self.client.cookies["csrftoken"].value

    def test_summary_and_run_detail(self):
        summary = self.client.get("/api/summary/")
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.json()["jobs"]["total"], 1)
        detail = self.client.get(f"/api/runs/{self.run.id}/")
        self.assertEqual(detail.json()["body"], "오늘의 문제")
        self.assertEqual(len(detail.json()["citations"]), 1)

    def test_saves_response_with_csrf(self):
        token = self.csrf_token()
        response = self.client.post(
            f"/api/runs/{self.run.id}/responses/",
            data=json.dumps({"question_key": "quiz-1", "answer": "My answer"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["answer"], "My answer")

    def test_updates_completion_state(self):
        reconcile_cron_runs([self.run.pk])
        token = self.csrf_token()
        response = self.client.patch(
            f"/api/runs/{self.run.id}/state/",
            data=json.dumps(
                {"completed": True, "bookmarked": True, "note": "복습 메모"}
            ),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["completed"])
        self.assertTrue(response.json()["bookmarked"])
        self.assertEqual(response.json()["note"], "복습 메모")
        self.assertTrue(
            KnowledgeConsumptionState.objects.get(
                knowledge_item__content_run=self.run
            ).completed_at
        )
        self.assertEqual(UserRunState.objects.count(), 0)

    def test_lists_free_question_items_in_newest_first_order(self):
        FreeQuestionMessage.objects.create(
            external_ts="200.200",
            thread_ts="200.100",
            role=FreeQuestionMessage.Role.ASSISTANT,
            content="두 번째 답변",
            generated_at=datetime(2026, 7, 15, 2, tzinfo=UTC),
        )
        FreeQuestionMessage.objects.create(
            external_ts="200.100",
            thread_ts="200.100",
            role=FreeQuestionMessage.Role.USER,
            content="첫 번째 질문",
            generated_at=datetime(2026, 7, 15, 1, tzinfo=UTC),
        )
        reconcile_slack_thread("200.100")

        response = self.client.get("/api/free-question/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 1)
        item = response.json()["results"][0]
        self.assertEqual(item["question_excerpt"], "첫 번째 질문")
        self.assertTrue(item["has_answer"])
        self.assertEqual(item["status"], KnowledgeItem.Status.PENDING)
