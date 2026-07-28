import hashlib
import os
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Category,
    ContentRun,
    CronJob,
    KnowledgeAsk,
    KnowledgeItem,
    QuizQuestion,
    QuizSession,
    ScheduleCategory,
    ScheduleEvent,
)
from .services import reconcile_cron_runs


DEMO_JOB_ID = "demo:knowledge"
DEMO_SCHEDULE_PREFIX = "demo:onboarding:"


def onboarding_status() -> dict:
    visible_items = KnowledgeItem.objects.filter(hidden_at__isnull=True).filter(
        Q(content_run__isnull=True) | Q(content_run__hidden_at__isnull=True)
    )
    demo_loaded = CronJob.objects.filter(external_id=DEMO_JOB_ID).exists()
    source_count = CronJob.objects.exclude(external_id=DEMO_JOB_ID).filter(
        disconnected_at__isnull=True,
        enabled=True,
    ).count()
    classified_count = visible_items.filter(
        status=KnowledgeItem.Status.CLASSIFIED
    ).count()
    checked_at = timezone.now()
    stale_verification = (
        Q(verification_status=KnowledgeItem.VerificationStatus.STALE)
        | Q(classification_stale_at__isnull=False)
        | Q(review_due_at__isnull=False, review_due_at__lte=checked_at)
    )
    verified_count = (
        visible_items.filter(
            verification_status=KnowledgeItem.VerificationStatus.VERIFIED
        )
        .exclude(stale_verification)
        .count()
    )
    quiz_count = QuizQuestion.objects.filter(
        publish_state=QuizQuestion.PublishState.PUBLISHED,
        is_active=True,
    ).count()
    steps = [
        {
            "key": "connect",
            "label": "Slack 소스 연결",
            "complete": source_count > 0,
            "href": "/admin/dashboard/cronjob/",
        },
        {
            "key": "sync",
            "label": "첫 지식 동기화",
            "complete": visible_items.exists(),
            "href": "/?view=operations",
        },
        {
            "key": "classify",
            "label": "지식 분류",
            "complete": classified_count > 0,
            "href": "/?view=library&status=pending",
        },
        {
            "key": "verify",
            "label": "지식 검증",
            "complete": verified_count > 0,
            "href": "/?view=library&verification=unverified",
        },
        {
            "key": "ask",
            "label": "지식에게 질문",
            "complete": KnowledgeAsk.objects.exists(),
            "href": "/ask",
        },
        {
            "key": "quiz",
            "label": "퀴즈 실행",
            "complete": quiz_count >= 10,
            "href": "/quiz",
        },
    ]
    return {
        "empty": not visible_items.exists(),
        "demo_loaded": demo_loaded,
        "completed_steps": sum(step["complete"] for step in steps),
        "total_steps": len(steps),
        "steps": steps,
        "configuration": {
            "slack_token_set": bool(os.getenv("SLACK_BOT_TOKEN", "").strip()),
            "slack_channels_set": bool(os.getenv("SLACK_KNOWLEDGE_CHANNELS", "").strip()),
            "llm_provider": os.getenv("LLM_PROVIDER", "anthropic").strip().lower(),
            "llm_key_set": bool(
                os.getenv("ANTHROPIC_API_KEY", "").strip()
                or os.getenv("OPENAI_API_KEY", "").strip()
            ),
        },
    }


def seed_demo_data() -> dict:
    checked_at = timezone.now()
    with transaction.atomic():
        categories = {
            "operations": _category_path("운영/배포"),
            "product": _category_path("제품/온보딩"),
            "english": _category_path("학습/언어/영어"),
        }
        job, _ = CronJob.objects.update_or_create(
            external_id=DEMO_JOB_ID,
            defaults={
                "name": "#demo-knowledge",
                "channel_id": "DEMO",
                "enabled": True,
                "state": "demo",
                "last_status": "success",
                "last_error": "",
                "last_run_at": checked_at,
                "last_import_count": 3,
                "disconnected_at": None,
            },
        )
        examples = (
            (
                "deployment",
                "배포 전 확인할 체크리스트",
                "운영 배포 전 담당자 승인을 받고 모니터링 대시보드와 롤백 절차를 확인합니다.",
                "operations",
            ),
            (
                "onboarding",
                "신규 사용자 온보딩 흐름",
                "Slack 연결, 첫 동기화, 지식 분류, 검증, 질문 순서로 온보딩을 진행합니다.",
                "product",
            ),
            (
                "english",
                "Incident response vocabulary",
                "An incident commander coordinates response, communicates status, and owns the timeline.",
                "english",
            ),
        )
        run_ids = []
        item_by_key = {}
        for index, (key, title, body, category_key) in enumerate(examples, start=1):
            run, _ = ContentRun.objects.update_or_create(
                job=job,
                external_ts=f"4102444800.{index:06d}",
                defaults={
                    "status": ContentRun.Status.SUCCESS,
                    "title": title,
                    "body": body,
                    "raw_text": body,
                    "error": "",
                    "structured_data": {
                        "source": "demo",
                        "demo_key": key,
                    },
                    "generated_at": checked_at - timedelta(hours=4 - index),
                    "hidden_at": None,
                },
            )
            run_ids.append(run.pk)
        reconcile_cron_runs(run_ids)
        for run, example in zip(
            ContentRun.objects.filter(pk__in=run_ids).order_by("external_ts"),
            examples,
            strict=True,
        ):
            key, _title, _body, category_key = example
            item = KnowledgeItem.objects.get(content_run=run)
            item.status = KnowledgeItem.Status.CLASSIFIED
            item.category = categories[category_key]
            item.classified_at = checked_at
            item.classification_model = "demo"
            item.classification_confidence = 1
            item.classification_reason = "Generated sample data"
            item.verification_status = KnowledgeItem.VerificationStatus.VERIFIED
            item.verified_at = checked_at
            item.review_due_at = checked_at + timedelta(days=90)
            item.verification_note = "Generated sample data"
            item.save()
            item_by_key[key] = item
        _seed_demo_quiz(item_by_key["english"], checked_at)
        _seed_demo_schedule(checked_at)
    return {
        "knowledge_items": len(examples),
        "quiz_questions": 10,
        "schedule_items": 2,
    }


def purge_demo_data() -> dict:
    with transaction.atomic():
        job = CronJob.objects.filter(external_id=DEMO_JOB_ID).first()
        deleted_questions = 0
        deleted_items = 0
        if job:
            item_ids = list(
                KnowledgeItem.objects.filter(content_run__job=job).values_list(
                    "pk",
                    flat=True,
                )
            )
            question_ids = list(
                QuizQuestion.objects.filter(
                    knowledge_item_id__in=item_ids
                ).values_list("pk", flat=True)
            )
            if question_ids:
                QuizSession.objects.filter(
                    items__question_id__in=question_ids
                ).distinct().delete()
                deleted_questions = len(question_ids)
                QuizQuestion.objects.filter(
                    pk__in=question_ids
                ).delete()
            deleted_items = len(item_ids)
            job.delete()
        deleted_schedule, _ = ScheduleEvent.objects.filter(
            source_hash__startswith=DEMO_SCHEDULE_PREFIX
        ).delete()
    return {
        "knowledge_items": deleted_items,
        "quiz_questions": deleted_questions,
        "schedule_items": deleted_schedule,
    }


def _category_path(path: str) -> Category:
    parent = None
    category = None
    parts = []
    for depth, name in enumerate(path.split("/"), start=1):
        parts.append(name)
        category, _ = Category.objects.get_or_create(
            path="/".join(parts),
            defaults={
                "name": name,
                "parent": parent,
                "depth": depth,
                "created_by": Category.CreatedBy.SYSTEM,
                "is_active": True,
            },
        )
        parent = category
    return category


def _seed_demo_quiz(item: KnowledgeItem, checked_at) -> None:
    choices = [
        {"id": "a", "text": "Coordinates response and communication"},
        {"id": "b", "text": "Only writes the final report"},
    ]
    for index in range(1, 11):
        prompt = f"Demo {index}: What does an incident commander do?"
        prompt_digest = hashlib.sha256(prompt.encode()).hexdigest()
        evidence = "An incident commander coordinates response"
        QuizQuestion.objects.update_or_create(
            knowledge_item=item,
            domain="english",
            difficulty=QuizQuestion.Difficulty.BEGINNER,
            prompt=prompt,
            defaults={
                "question_type": QuizQuestion.QuestionType.SINGLE_CHOICE,
                "choices": choices,
                "correct_choice_ids": ["a"],
                "explanation": "The source defines the incident commander role.",
                "evidence_excerpt": evidence,
                "evidence_digest": hashlib.sha256(evidence.encode()).hexdigest(),
                "source_hash": item.source_hash,
                "generator_version": "demo-v1",
                "model_name": "demo",
                "prompt_version": "demo-v1",
                "prompt_digest": prompt_digest,
                "publish_state": QuizQuestion.PublishState.PUBLISHED,
                "is_active": True,
                "published_at": checked_at,
                "generated_at": checked_at,
            },
        )


def _seed_demo_schedule(checked_at) -> None:
    category = ScheduleCategory.objects.filter(is_fallback=True).first()
    if category is None:
        category, _ = ScheduleCategory.objects.get_or_create(
            name="기타",
            defaults={
                "keywords": [],
                "sort_order": 999,
                "is_fallback": True,
            },
        )
    ScheduleEvent.objects.update_or_create(
        source_hash=f"{DEMO_SCHEDULE_PREFIX}todo",
        defaults={
            "title": "지식 검증 큐 확인",
            "item_type": ScheduleEvent.ItemType.TODO,
            "todo_category": category,
            "starts_at": checked_at + timedelta(days=1),
            "ends_at": None,
            "notes": "Generated sample data",
            "source_type": ScheduleEvent.SourceType.MANUAL,
        },
    )
    ScheduleEvent.objects.update_or_create(
        source_hash=f"{DEMO_SCHEDULE_PREFIX}schedule",
        defaults={
            "title": "주간 지식 운영 리뷰",
            "item_type": ScheduleEvent.ItemType.SCHEDULE,
            "todo_category": None,
            "starts_at": checked_at + timedelta(days=2),
            "ends_at": checked_at + timedelta(days=2, hours=1),
            "notes": "Generated sample data",
            "source_type": ScheduleEvent.SourceType.MANUAL,
        },
    )
