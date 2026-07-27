from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from .models import KnowledgeItem, QuizProgress, QuizQuestion, QuizSession, QuizSessionItem
from .quiz_sessions import QuizApiError, knowledge_detail_url


SEOUL = ZoneInfo("Asia/Seoul")
REVIEW_INTERVALS = {
    QuizProgress.Stage.RESET: QuizProgress.Stage.THREE_DAYS,
    QuizProgress.Stage.ONE_DAY: QuizProgress.Stage.THREE_DAYS,
    QuizProgress.Stage.THREE_DAYS: QuizProgress.Stage.SEVEN_DAYS,
    QuizProgress.Stage.SEVEN_DAYS: QuizProgress.Stage.FOURTEEN_DAYS,
    QuizProgress.Stage.FOURTEEN_DAYS: QuizProgress.Stage.THIRTY_DAYS,
}
STAGE_DAYS = {
    QuizProgress.Stage.ONE_DAY: 1,
    QuizProgress.Stage.THREE_DAYS: 3,
    QuizProgress.Stage.SEVEN_DAYS: 7,
    QuizProgress.Stage.FOURTEEN_DAYS: 14,
    QuizProgress.Stage.THIRTY_DAYS: 30,
}
TODAY_GOAL_TARGET = 10
REVIEWABLE_PROGRESS_FILTER = (
    Q(progress__wrong_count__gt=0)
    | Q(progress__manual_wrong_note_at__isnull=False)
    | Q(progress__next_review_at__isnull=False)
)


def eligible_review_questions():
    return (
        QuizQuestion.objects.filter(
            publish_state=QuizQuestion.PublishState.PUBLISHED,
            is_active=True,
            knowledge_item__status=KnowledgeItem.Status.CLASSIFIED,
            knowledge_item__hidden_at__isnull=True,
            knowledge_item__consumption_state__archived_at__isnull=True,
            source_hash=F("knowledge_item__source_hash"),
        )
        .filter(
            Q(knowledge_item__content_run__isnull=True)
            | Q(knowledge_item__content_run__hidden_at__isnull=True)
        )
        .select_related("knowledge_item", "knowledge_item__content_run", "progress")
    )


def apply_answer_progress(
    question: QuizQuestion,
    *,
    correct: bool,
    answered_at,
    mode: str,
) -> dict:
    progress, _created = QuizProgress.objects.select_for_update().get_or_create(
        question=question
    )
    if correct:
        progress.correct_streak += 1
        progress.last_answered_at = answered_at
        if mode == QuizSession.Mode.REVIEW and _is_due(progress, answered_at):
            _advance_due_correct(progress, answered_at)
    else:
        progress.stage = QuizProgress.Stage.RESET
        progress.wrong_count += 1
        progress.correct_streak = 0
        progress.next_review_at = answered_at + timedelta(days=1)
        progress.last_answered_at = answered_at
        progress.mastered_at = None
    progress.save(
        update_fields=[
            "stage",
            "wrong_count",
            "correct_streak",
            "next_review_at",
            "last_answered_at",
            "mastered_at",
            "updated_at",
        ]
    )
    return progress_payload(progress)


def _is_due(progress: QuizProgress, now) -> bool:
    return progress.next_review_at is not None and progress.next_review_at <= now


def _advance_due_correct(progress: QuizProgress, now) -> None:
    if progress.stage == QuizProgress.Stage.THIRTY_DAYS:
        progress.mastered_at = now
        progress.next_review_at = None
        return
    next_stage = REVIEW_INTERVALS.get(progress.stage, QuizProgress.Stage.THREE_DAYS)
    progress.stage = next_stage
    progress.next_review_at = now + timedelta(days=STAGE_DAYS[next_stage])


def progress_payload(progress: QuizProgress) -> dict:
    return {
        "stage": progress.stage,
        "wrong_count": progress.wrong_count,
        "correct_streak": progress.correct_streak,
        "next_review_at": _iso(progress.next_review_at),
        "last_answered_at": _iso(progress.last_answered_at),
        "mastered_at": _iso(progress.mastered_at),
        "manual_wrong_note_at": _iso(progress.manual_wrong_note_at),
    }


def review_payload(params: dict) -> dict:
    domain = params.get("domain", "")
    difficulty = params.get("difficulty", "")
    due_only = params.get("due_only", "")
    if domain and domain not in {"english", "japanese", "aws_saa"}:
        raise QuizApiError(400, "invalid_filter")
    if difficulty and difficulty not in {"beginner", "intermediate", "advanced"}:
        raise QuizApiError(400, "invalid_filter")
    due_filter = _parse_due_only(due_only)
    now = timezone.now()
    queryset = eligible_review_questions().filter(REVIEWABLE_PROGRESS_FILTER)
    if domain:
        queryset = queryset.filter(domain=domain)
    if difficulty:
        queryset = queryset.filter(difficulty=difficulty)
    due_count = queryset.filter(progress__next_review_at__lte=now).count()
    if due_filter:
        queryset = queryset.filter(progress__next_review_at__lte=now)
    queryset = queryset.order_by("progress__next_review_at", "id")
    questions = list(queryset)
    stage_counts = dict(
        queryset.order_by()
        .values("progress__stage")
        .annotate(count=Count("id"))
        .values_list("progress__stage", "count")
    )
    return {
        "items": [review_item_payload(question) for question in questions],
        "due_count": due_count,
        "stage_counts": stage_counts,
        "today_goal": today_goal_payload(now),
        "streak": streak_payload(now),
        "last_reviewed_at": _iso(
            QuizProgress.objects.filter(last_answered_at__isnull=False)
            .order_by("-last_answered_at")
            .values_list("last_answered_at", flat=True)
            .first()
        ),
    }


def _parse_due_only(value: str) -> bool:
    if value in ("", None):
        return False
    if value == "1":
        return True
    if value == "0":
        return False
    raise QuizApiError(400, "invalid_filter")


def review_item_payload(question: QuizQuestion) -> dict:
    progress = question.progress
    return {
        "question_id": question.pk,
        "question_type": question.question_type,
        "domain": question.domain,
        "difficulty": question.difficulty,
        **progress_payload(progress),
        "prior_feedback": prior_feedback(question),
        "source": {
            "title": question.knowledge_item.title,
            "detail_url": knowledge_detail_url(question.knowledge_item),
        },
    }


def prior_feedback(question: QuizQuestion) -> dict | None:
    item = (
        QuizSessionItem.objects.filter(question=question, answered_at__isnull=False)
        .select_related("session")
        .order_by("-answered_at", "-id")
        .first()
    )
    if not item:
        return None
    return {
        "correct": item.correct,
        "answered_at": _iso(item.answered_at),
        "session_id": str(item.session.session_id),
    }


def update_manual_wrong_note(question_id: int, data: dict) -> dict:
    if not isinstance(data, dict) or "manual_wrong_note" not in data:
        raise QuizApiError(400, "invalid_note")
    if set(data) - {"manual_wrong_note", "note"}:
        raise QuizApiError(400, "invalid_note")
    manual_wrong_note = data["manual_wrong_note"]
    if type(manual_wrong_note) is not bool:
        raise QuizApiError(400, "invalid_note")
    note = data.get("note", "")
    if note is not None and (not isinstance(note, str) or len(note) > 2000):
        raise QuizApiError(400, "invalid_note")
    with transaction.atomic():
        question = (
            QuizQuestion.objects.select_for_update()
            .filter(pk=question_id)
            .select_related("knowledge_item", "knowledge_item__content_run")
            .first()
        )
        if question is None:
            raise QuizApiError(404, "quiz_question_not_found")
        if not eligible_review_questions().filter(pk=question.pk).exists():
            raise QuizApiError(409, "quiz_question_locked")
        progress = (
            QuizProgress.objects.select_for_update()
            .filter(question=question)
            .first()
        )
        if not progress:
            if not manual_wrong_note:
                return {
                    "question_id": question_id,
                    "progress": progress_payload(QuizProgress(question=question)),
                }
            progress = QuizProgress(question=question)
        now = timezone.now()
        created = progress.pk is None
        if manual_wrong_note:
            if progress.manual_wrong_note_at:
                return {"question_id": question_id, "progress": progress_payload(progress)}
            progress.stage = QuizProgress.Stage.RESET
            progress.wrong_count += 1
            progress.correct_streak = 0
            progress.manual_wrong_note_at = now
            progress.next_review_at = now + timedelta(days=1)
            progress.mastered_at = None
        else:
            progress.manual_wrong_note_at = None
        update_fields = [
            "stage",
            "wrong_count",
            "correct_streak",
            "manual_wrong_note_at",
            "next_review_at",
            "mastered_at",
            "updated_at",
        ]
        progress.save(update_fields=None if created else update_fields)
    return {"question_id": question_id, "progress": progress_payload(progress)}


def today_goal_payload(now) -> dict:
    today = timezone.localtime(now, SEOUL).date()
    completed = sum(
        1
        for answered_at in QuizSessionItem.objects.filter(
            answered_at__isnull=False
        ).values_list("answered_at", flat=True)
        if timezone.localtime(answered_at, SEOUL).date() == today
    )
    return {
        "target": TODAY_GOAL_TARGET,
        "completed": completed,
        "remaining": max(TODAY_GOAL_TARGET - completed, 0),
    }


def streak_payload(now) -> dict:
    completed_dates = {
        timezone.localtime(value, SEOUL).date()
        for value in QuizSession.objects.filter(
            status=QuizSession.Status.COMPLETED,
            completed_at__isnull=False,
        ).values_list("completed_at", flat=True)
    }
    current = timezone.localtime(now, SEOUL).date()
    if current not in completed_dates:
        current = current - timedelta(days=1)
    days = 0
    while current in completed_dates:
        days += 1
        current = current - timedelta(days=1)
    return {"current_days": days}


def _iso(value) -> str | None:
    return value.isoformat() if value else None
