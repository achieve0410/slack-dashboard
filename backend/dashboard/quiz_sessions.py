from dataclasses import dataclass
from uuid import UUID

from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from .models import (
    KnowledgeItem,
    QuizProgress,
    QuizQuestion,
    QuizDomainConfig,
    QuizSession,
    QuizSessionItem,
)


REQUIRED_COUNT = 10
SUPPORTED_DIFFICULTIES = ("beginner", "intermediate", "advanced")
SUPPORTED_MODES = ("new", "review", "wrong")
SUPPORTED_TYPES = ("single_choice", "multiple_select")


class QuizApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "", **payload):
        self.status = status
        self.code = code
        self.message = message or code
        self.payload = payload
        super().__init__(self.message)

    def response_payload(self) -> dict:
        return {"code": self.code, "error": self.message, **self.payload}


@dataclass(frozen=True)
class NormalizedAnswer:
    choice_ids: list[str]


def knowledge_detail_url(item: KnowledgeItem) -> str:
    if item.source_type == KnowledgeItem.SourceType.CRON:
        return f"/runs/{item.content_run_id}"
    return f"/knowledge/{item.id}"


def eligible_questions():
    return (
        QuizQuestion.objects.filter(
            publish_state=QuizQuestion.PublishState.PUBLISHED,
            is_active=True,
            domain__in=QuizDomainConfig.objects.filter(enabled=True).values("slug"),
            knowledge_item__status=KnowledgeItem.Status.CLASSIFIED,
            knowledge_item__hidden_at__isnull=True,
            knowledge_item__consumption_state__archived_at__isnull=True,
        )
        .filter(
            Q(knowledge_item__content_run__isnull=True)
            | Q(knowledge_item__content_run__hidden_at__isnull=True)
        )
        .filter(source_hash=F("knowledge_item__source_hash"))
        .select_related("knowledge_item", "knowledge_item__content_run")
    )


def catalog_payload() -> dict:
    questions = eligible_questions()
    domain_configs = list(QuizDomainConfig.objects.filter(enabled=True))
    counts = {
        f"{row['domain']}:{row['difficulty']}": row["count"]
        for row in questions.values("domain", "difficulty")
        .annotate(count=Count("id"))
        .order_by("domain", "difficulty")
    }
    total = sum(counts.values())
    return {
        "domains": [config.slug for config in domain_configs],
        "domain_configs": [
            {
                "slug": config.slug,
                "label": config.label,
                "category_path": config.category_path,
                "question_types": config.allowed_question_types,
                "requires_allowlist": config.requires_allowlist,
            }
            for config in domain_configs
        ],
        "difficulty_levels": list(SUPPORTED_DIFFICULTIES),
        "question_types": list(SUPPORTED_TYPES),
        "available_counts": counts,
        "allowlist_version": "",
        "published_at": questions.order_by("-published_at").values_list(
            "published_at",
            flat=True,
        ).first(),
        "empty_state": total == 0,
    }


def parse_session_request(data: dict) -> tuple[str, str, str]:
    if not isinstance(data, dict):
        raise QuizApiError(400, "invalid_request", "요청 본문은 JSON 객체여야 합니다.")
    if set(data) != {"domain", "difficulty", "mode"}:
        raise QuizApiError(400, "invalid_request", "domain, difficulty, mode가 필요합니다.")
    domain = data["domain"]
    difficulty = data["difficulty"]
    mode = data["mode"]
    if not QuizDomainConfig.objects.filter(slug=domain, enabled=True).exists():
        raise QuizApiError(400, "invalid_domain")
    if difficulty not in SUPPORTED_DIFFICULTIES:
        raise QuizApiError(400, "invalid_difficulty")
    if mode not in SUPPORTED_MODES:
        raise QuizApiError(400, "invalid_mode")
    return domain, difficulty, mode


def create_session(data: dict) -> tuple[QuizSession, dict]:
    domain, difficulty, mode = parse_session_request(data)
    with transaction.atomic():
        available_count = eligible_questions().filter(
            domain=domain,
            difficulty=difficulty,
        ).count()
        pool = select_session_questions(domain, difficulty, mode)
        if len(pool) < REQUIRED_COUNT:
            raise QuizApiError(
                409,
                "quiz_pool_shortage",
                required_count=REQUIRED_COUNT,
                available_count=available_count,
                domain=domain,
                difficulty=difficulty,
                mode=mode,
            )
        session = QuizSession.objects.create(
            domain=domain,
            difficulty=difficulty,
            mode=mode,
            required_count=REQUIRED_COUNT,
        )
        QuizSessionItem.objects.bulk_create(
            [
                QuizSessionItem(session=session, question=question, position=index)
                for index, question in enumerate(pool, start=1)
            ]
        )
    payload = session_payload(session.session_id)
    payload["available_count"] = available_count
    return session, payload


def select_session_questions(
    domain: str,
    difficulty: str,
    mode: str,
) -> list[QuizQuestion]:
    bank = list(
        eligible_questions()
        .filter(domain=domain, difficulty=difficulty)
        .order_by("id")
    )
    if len(bank) < REQUIRED_COUNT:
        return bank
    if mode == QuizSession.Mode.REVIEW:
        priority = list(
            eligible_questions()
            .filter(
                domain=domain,
                difficulty=difficulty,
                progress__next_review_at__isnull=False,
                progress__next_review_at__lte=timezone.now(),
            )
            .order_by("progress__next_review_at", "id")
        )
    elif mode == QuizSession.Mode.WRONG:
        priority = list(
            eligible_questions()
            .filter(domain=domain, difficulty=difficulty)
            .filter(
                Q(progress__wrong_count__gt=0)
                | Q(progress__manual_wrong_note_at__isnull=False)
            )
            .order_by("progress__next_review_at", "id")
        )
    else:
        priority = []
    by_id = {question.pk: question for question in bank}
    selected = []
    seen = set()
    for question in priority:
        if question.pk in by_id and question.pk not in seen:
            selected.append(by_id[question.pk])
            seen.add(question.pk)
    for question in bank:
        if question.pk not in seen:
            selected.append(question)
            seen.add(question.pk)
        if len(selected) == REQUIRED_COUNT:
            break
    return selected[:REQUIRED_COUNT]


def session_history_payload(params: dict) -> dict:
    limit = parse_history_limit(params.get("limit", "20"))
    sessions = (
        QuizSession.objects.annotate(
            answered_count=Count("items", filter=Q(items__answered_at__isnull=False)),
            total_count=Count("items"),
            score=Count("items", filter=Q(items__correct=True)),
        )
        .order_by("-started_at", "-id")[:limit]
    )
    return {"results": [session_history_item_payload(session) for session in sessions]}


def parse_history_limit(value) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as error:
        raise QuizApiError(400, "invalid_limit") from error
    if str(value) != str(limit) or not 1 <= limit <= 50:
        raise QuizApiError(400, "invalid_limit")
    return limit


def session_history_item_payload(session: QuizSession) -> dict:
    return {
        "session_id": str(session.session_id),
        "status": session.status,
        "domain": session.domain,
        "difficulty": session.difficulty,
        "mode": session.mode,
        "answered_count": session.answered_count,
        "total_count": session.total_count,
        "score": session.score,
        "started_at": session.started_at,
        "completed_at": session.completed_at,
    }


def session_payload(session_id: UUID | str) -> dict:
    session = _session(session_id)
    items = list(_session_items(session))
    current = _current_item(items)
    return {
        "session_id": str(session.session_id),
        "status": session.status,
        "domain": session.domain,
        "difficulty": session.difficulty,
        "mode": session.mode,
        "required_count": session.required_count,
        "current_item": pre_submit_item_payload(current) if current else None,
        "result": session_summary(session, items) if session.status == QuizSession.Status.COMPLETED else None,
        "review_summary": review_summary(items),
        "progress": progress_count_payload(items),
        "items": [safe_session_item_payload(item) for item in items],
    }


def result_payload(session_id: UUID | str) -> dict:
    session = _session(session_id)
    items = list(_session_items(session))
    if session.status != QuizSession.Status.COMPLETED:
        raise QuizApiError(409, "quiz_session_incomplete")
    summary = session_summary(session, items)
    return {
        "session_id": str(session.session_id),
        "status": session.status,
        "score": summary["score"],
        "correct_count": summary["correct_count"],
        "incorrect_count": summary["incorrect_count"],
        "mastered_count": mastered_count(items),
        "item_results": [result_item_payload(item) for item in items],
        "review_summary": review_summary(items),
        "completed_at": session.completed_at,
    }


def answer_item(session_id: UUID | str, item_id: int, data: dict) -> dict:
    if not isinstance(data, dict) or set(data) != {"choice_ids"}:
        raise QuizApiError(400, "invalid_request", "choice_ids가 필요합니다.")
    with transaction.atomic():
        session = _session(session_id, for_update=True)
        items = list(_session_items(session, for_update=True))
        try:
            item = next(candidate for candidate in items if candidate.pk == item_id)
        except StopIteration as error:
            raise QuizApiError(409, "quiz_item_not_found") from error
        normalized = normalize_answer(data["choice_ids"], item.question)
        if item.answered_at:
            if item.accepted_choice_ids == normalized.choice_ids:
                return item.feedback_snapshot
            raise QuizApiError(409, "quiz_answer_conflict")

        current = _current_item(items)
        if current is None:
            raise QuizApiError(409, "quiz_item_locked")
        if current.pk != item.pk:
            raise QuizApiError(409, "quiz_item_locked")

        correct_ids = sorted(item.question.correct_choice_ids)
        correct = normalized.choice_ids == correct_ids
        answered_at = timezone.now()
        item.accepted_choice_ids = normalized.choice_ids
        item.answered_at = answered_at
        item.correct = correct
        progress = update_progress(item.question, correct, answered_at, mode=session.mode)
        next_item = _next_item(items, item)
        if next_item is None:
            session.status = QuizSession.Status.COMPLETED
            session.completed_at = answered_at
            session.save(update_fields=["status", "completed_at", "updated_at"])
        item.feedback_snapshot = answer_payload(
            item,
            accepted_choice_ids=normalized.choice_ids,
            correct=correct,
            progress=progress,
            next_item=next_item,
            session=session,
            items=items,
        )
        item.save(
            update_fields=[
                "accepted_choice_ids",
                "answered_at",
                "correct",
                "feedback_snapshot",
                "updated_at",
            ]
        )
        return item.feedback_snapshot


def normalize_answer(value, question: QuizQuestion) -> NormalizedAnswer:
    if not isinstance(value, list) or not value:
        raise QuizApiError(400, "invalid_choice_ids")
    if any(not isinstance(choice_id, str) or not choice_id for choice_id in value):
        raise QuizApiError(400, "invalid_choice_ids")
    if len(value) != len(set(value)):
        raise QuizApiError(400, "invalid_choice_ids")
    choice_ids = sorted(value)
    valid_ids = {choice["id"] for choice in question.choices}
    if not set(choice_ids).issubset(valid_ids):
        raise QuizApiError(400, "invalid_choice_ids")
    if question.question_type == QuizQuestion.QuestionType.SINGLE_CHOICE and len(choice_ids) != 1:
        raise QuizApiError(400, "invalid_choice_ids")
    if question.question_type == QuizQuestion.QuestionType.MULTIPLE_SELECT and len(choice_ids) < 1:
        raise QuizApiError(400, "invalid_choice_ids")
    return NormalizedAnswer(choice_ids)


def update_progress(
    question: QuizQuestion,
    correct: bool,
    answered_at,
    *,
    mode: str = QuizSession.Mode.NEW,
):
    from .quiz_review import apply_answer_progress

    return apply_answer_progress(
        question,
        correct=correct,
        answered_at=answered_at,
        mode=mode,
    )


def pre_submit_item_payload(item: QuizSessionItem | None) -> dict | None:
    if item is None:
        return None
    question = item.question
    return {
        "id": item.pk,
        "position": item.position,
        "question_type": question.question_type,
        "prompt": question.prompt,
        "choices": [
            {"id": choice["id"], "label": choice["text"]}
            for choice in question.choices
        ],
        "domain": question.domain,
        "difficulty": question.difficulty,
    }


def safe_session_item_payload(item: QuizSessionItem) -> dict:
    return {
        "id": item.pk,
        "position": item.position,
        "answered": bool(item.answered_at),
        "correct": item.correct if item.answered_at else None,
    }


def answer_payload(
    item: QuizSessionItem,
    *,
    accepted_choice_ids: list[str],
    correct: bool,
    progress: dict,
    next_item: QuizSessionItem | None,
    session: QuizSession,
    items: list[QuizSessionItem],
) -> dict:
    question = item.question
    source = source_payload(question.knowledge_item)
    return {
        "accepted_choice_ids": accepted_choice_ids,
        "correct": correct,
        "correct_choice_ids": sorted(question.correct_choice_ids),
        "explanation": question.explanation,
        "source": source,
        "progress": progress,
        "next_item": pre_submit_item_payload(next_item),
        "session_summary": session_summary(session, items),
    }


def source_payload(item: KnowledgeItem) -> dict:
    return {
        "title": item.title,
        "detail_url": knowledge_detail_url(item),
        "source_type": item.source_type,
        "source_key": item.source_key,
    }


def result_item_payload(item: QuizSessionItem) -> dict:
    snapshot = item.feedback_snapshot if isinstance(item.feedback_snapshot, dict) else {}
    return {
        "item_id": item.pk,
        "position": item.position,
        "question_id": item.question_id,
        "prompt": item.question.prompt,
        "accepted_choice_ids": item.accepted_choice_ids,
        "correct": item.correct,
        "correct_choice_ids": snapshot.get("correct_choice_ids", sorted(item.question.correct_choice_ids)),
        "explanation": snapshot.get("explanation", item.question.explanation),
        "source": snapshot.get("source", source_payload(item.question.knowledge_item)),
    }


def session_summary(session: QuizSession, items: list[QuizSessionItem]) -> dict:
    answered = [item for item in items if item.answered_at]
    correct_count = sum(1 for item in answered if item.correct)
    incorrect_count = sum(1 for item in answered if item.correct is False)
    total = len(items)
    return {
        "status": session.status,
        "score": correct_count,
        "correct_count": correct_count,
        "incorrect_count": incorrect_count,
        "answered_count": len(answered),
        "total_count": total,
        "completed": session.status == QuizSession.Status.COMPLETED,
    }


def review_summary(items: list[QuizSessionItem]) -> dict:
    return {
        "wrong_count": sum(1 for item in items if item.correct is False),
        "answered_count": sum(1 for item in items if item.answered_at),
    }


def mastered_count(items: list[QuizSessionItem]) -> int:
    question_ids = [item.question_id for item in items]
    if not question_ids:
        return 0
    return QuizProgress.objects.filter(
        question_id__in=question_ids,
        mastered_at__isnull=False,
    ).count()


def progress_count_payload(items: list[QuizSessionItem]) -> dict:
    return {
        "answered_count": sum(1 for item in items if item.answered_at),
        "total_count": len(items),
    }


def _session(session_id: UUID | str, *, for_update: bool = False) -> QuizSession:
    queryset = QuizSession.objects
    if for_update:
        queryset = queryset.select_for_update()
    try:
        return queryset.get(session_id=session_id)
    except (QuizSession.DoesNotExist, ValueError) as error:
        raise QuizApiError(404, "quiz_session_not_found") from error


def _session_items(
    session: QuizSession,
    *,
    for_update: bool = False,
):
    queryset = QuizSessionItem.objects
    if for_update:
        queryset = queryset.select_for_update()
    return (
        queryset.filter(session=session)
        .select_related("question", "question__knowledge_item", "question__knowledge_item__content_run")
        .order_by("position", "id")
    )


def _current_item(items: list[QuizSessionItem]) -> QuizSessionItem | None:
    return next((item for item in items if not item.answered_at), None)


def _next_item(
    items: list[QuizSessionItem],
    current: QuizSessionItem,
) -> QuizSessionItem | None:
    return next(
        (item for item in items if item.position > current.position and not item.answered_at),
        None,
    )
