import hashlib
import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from .models import (
    ContentRun,
    FreeQuestionMessage,
    KnowledgeItem,
)


SLACK_RESPONSE_RE = re.compile(
    r"^Cronjob Response:\s*(?P<title>.*?)\s*\(job_id:\s*(?P<job_id>[^)]+)\)\s*-+\s*(?P<body>.*)$",
    re.DOTALL,
)
SLACK_LINK_RE = re.compile(r"<(https?://[^>|]+)(?:\|([^>]+))?>")


@dataclass(frozen=True)
class ParsedSlackRun:
    job_id: str
    title: str
    body: str
    status: str
    error: str


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def slack_ts_to_datetime(value: str) -> datetime:
    return datetime.fromtimestamp(float(value), tz=UTC)


def parse_slack_response(text: str) -> ParsedSlackRun | None:
    match = SLACK_RESPONSE_RE.match(text.strip())
    if not match:
        return None

    body = match.group("body").strip()
    failed = ":warning:" in body or re.search(r"\b(?:cron|job).*?failed:", body, re.I)
    return ParsedSlackRun(
        job_id=match.group("job_id").strip(),
        title=html.unescape(match.group("title").strip()),
        body=body,
        status="failed" if failed else "success",
        error=body if failed else "",
    )


def extract_citations(text: str) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    seen: set[str] = set()
    for url, label in SLACK_LINK_RE.findall(text):
        url = html.unescape(url)
        if url in seen:
            continue
        seen.add(url)
        citations.append({"url": url, "title": html.unescape(label or url)})
    return citations


def source_hash(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def summarize(value: str) -> str:
    normalized_lines = [" ".join(line.split()) for line in value.splitlines()]
    normalized = "\n".join(line for line in normalized_lines).strip()
    if len(normalized) <= 600:
        return normalized

    summary_blocks: list[str] = []
    current_length = 0
    omitted_blocks = False
    for block in re.split(r"\n{2,}", normalized):
        block = block.strip()
        if not block:
            continue
        proposed_length = current_length + len(block) + (2 if summary_blocks else 0)
        if proposed_length > 600:
            omitted_blocks = True
            break
        summary_blocks.append(block)
        current_length = proposed_length

    if summary_blocks:
        if omitted_blocks and re.match(r"^#{1,3}\s+", summary_blocks[-1]):
            summary_blocks.pop()
        return "\n\n".join(summary_blocks)

    summary_lines: list[str] = []
    current_length = 0
    for line in normalized.splitlines():
        proposed_length = current_length + len(line) + (1 if summary_lines else 0)
        if proposed_length > 600:
            break
        summary_lines.append(line)
        current_length = proposed_length

    if summary_lines:
        return "\n".join(summary_lines)
    return normalized[:600]


def _save_changed(instance, updates: dict) -> bool:
    changed_fields = []
    for field, value in updates.items():
        if getattr(instance, field) != value:
            setattr(instance, field, value)
            changed_fields.append(field)
    if not changed_fields:
        return False
    instance.save(update_fields=[*changed_fields, "updated_at"])
    return True


def reconcile_cron_runs(run_ids: Iterable[int] | None = None) -> dict[str, int]:
    queryset = ContentRun.objects.order_by("pk")
    if run_ids is not None:
        queryset = queryset.filter(pk__in=set(run_ids))
    ids = list(queryset.values_list("pk", flat=True))

    stats = {
        "created": 0,
        "updated": 0,
        "deleted": 0,
        "unchanged": 0,
        "source_resets": 0,
        "manual_overrides": 0,
    }
    for run_id in ids:
        with transaction.atomic():
            run = (
                ContentRun.objects.select_for_update()
                .select_related("job")
                .get(pk=run_id)
            )
            existing = (
                KnowledgeItem.objects.select_for_update()
                .filter(content_run=run, source_type=KnowledgeItem.SourceType.CRON)
                .first()
            )
            if run.status != ContentRun.Status.SUCCESS:
                if existing:
                    existing.delete()
                    stats["deleted"] += 1
                else:
                    stats["unchanged"] += 1
                continue

            key = f"cron:{run.pk}"
            current_hash = source_hash(run.title, run.body or "")
            if not existing:
                has_body = bool((run.body or "").strip())
                KnowledgeItem.objects.create(
                    source_type=KnowledgeItem.SourceType.CRON,
                    source_key=key,
                    content_run=run,
                    category=None,
                    status=(
                        KnowledgeItem.Status.PENDING
                        if has_body
                        else KnowledgeItem.Status.NEEDS_REVIEW
                    ),
                    title=run.title,
                    summary=summarize(run.body or ""),
                    question="",
                    answer="",
                    source_hash=current_hash,
                    generated_at=run.generated_at,
                    hidden_at=run.hidden_at,
                    classification_model="",
                    classification_confidence=None,
                    classification_reason=(
                        "" if has_body else "Authoritative Cron body is empty."
                    ),
                    classified_at=None,
                )
                stats["created"] += 1
                continue

            base_updates = {
                "source_key": key,
                "content_run_id": run.pk,
                "generated_at": run.generated_at,
                "hidden_at": existing.hidden_at or run.hidden_at,
            }
            source_changed = existing.source_hash != current_hash
            if source_changed:
                base_updates["verification_status"] = (
                    KnowledgeItem.VerificationStatus.STALE
                    if existing.verified_at
                    else KnowledgeItem.VerificationStatus.UNVERIFIED
                )
            manual_override = (
                existing.classification_model == "manual"
                and existing.reviewed_at is not None
                and existing.status == KnowledgeItem.Status.CLASSIFIED
                and existing.category_id is not None
            )
            if manual_override:
                stats["manual_overrides"] += 1
                updates = {
                    **base_updates,
                    "title": run.title,
                    "summary": summarize(run.body or ""),
                    "question": "",
                    "answer": "",
                    "source_hash": current_hash,
                }
            elif source_changed:
                stats["source_resets"] += 1
                has_body = bool((run.body or "").strip())
                updates = {
                    **base_updates,
                    "category_id": None,
                    "status": (
                        KnowledgeItem.Status.PENDING
                        if has_body
                        else KnowledgeItem.Status.NEEDS_REVIEW
                    ),
                    "title": run.title,
                    "summary": summarize(run.body or ""),
                    "question": "",
                    "answer": "",
                    "source_hash": current_hash,
                    "classification_model": "",
                    "classification_confidence": None,
                    "classification_reason": (
                        "" if has_body else "Authoritative Cron body is empty."
                    ),
                    "classified_at": None,
                    "reviewed_by_id": None,
                    "reviewed_at": None,
                }
            else:
                updates = base_updates

            if _save_changed(existing, updates):
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1
    return stats


def _provisional_title(question: str) -> str:
    for raw_line in question.splitlines():
        line = re.sub(r"<@[A-Z0-9]+>", "", raw_line).strip()
        line = SLACK_LINK_RE.sub(lambda match: match.group(2) or match.group(1), line).strip()
        if not line or re.fullmatch(r"https?://\S+", line):
            continue
        return line[:250]
    fallback = re.sub(r"<@[A-Z0-9]+>", "", question).strip()
    return fallback[:250]


def _thread_question(messages: list[FreeQuestionMessage]) -> str:
    contents = [message.content.strip() for message in messages if message.content.strip()]
    if len(contents) == 1:
        return contents[0]
    blocks = []
    for index, content in enumerate(contents):
        label = "초기 요청" if index == 0 else f"후속 요청 {index}"
        blocks.append(f"## {label}\n\n{content}")
    return "\n\n".join(blocks)


def slack_source_url(workspace_url: str, channel_id: str, thread_ts: str) -> str:
    workspace = (workspace_url or "").strip().rstrip("/")
    if not workspace or not channel_id or not re.fullmatch(r"\d+\.\d+", thread_ts or ""):
        return ""
    return f"{workspace}/archives/{channel_id}/p{thread_ts.replace('.', '')}"


def reconcile_slack_thread(thread_ts: str, *, workspace_url: str = "") -> dict[str, int]:
    stats = {
        "created": 0,
        "updated": 0,
        "deleted": 0,
        "unchanged": 0,
        "orphan_messages": 0,
        "source_resets": 0,
        "manual_overrides": 0,
    }
    if not thread_ts:
        return stats

    with transaction.atomic():
        messages = list(
            FreeQuestionMessage.objects.select_for_update()
            .filter(thread_ts=thread_ts)
            .order_by("generated_at", "id")
        )
        FreeQuestionMessage.objects.filter(thread_ts=thread_ts).update(
            knowledge_item=None
        )
        for message in messages:
            message.knowledge_item_id = None

        question_messages = [
            message
            for message in messages
            if message.role == FreeQuestionMessage.Role.USER and message.content.strip()
        ]
        if question_messages:
            root_question = question_messages[0]
            root_index = messages.index(root_question)
            included_messages = messages[root_index:]
            latest_question_index = max(
                index
                for index, message in enumerate(included_messages)
                if message.role == FreeQuestionMessage.Role.USER
                and message.content.strip()
            )
            answer_messages = [
                message
                for message in included_messages[latest_question_index + 1 :]
                if message.role == FreeQuestionMessage.Role.ASSISTANT
                and message.content.strip()
            ]
            key = f"slack:{thread_ts}:{root_question.external_ts}"
            question = _thread_question(question_messages)
            answer = answer_messages[-1].content.strip() if answer_messages else ""
            current_hash = source_hash(question, answer)
            channel_id = next(
                (message.channel_id for message in messages if message.channel_id), ""
            )
            source_url = slack_source_url(workspace_url, channel_id, thread_ts)
            existing = (
                KnowledgeItem.objects.select_for_update()
                .filter(source_key=key, source_type=KnowledgeItem.SourceType.SLACK_QA)
                .first()
            )
            waiting = not answer
            if not existing:
                existing = KnowledgeItem.objects.create(
                    source_type=KnowledgeItem.SourceType.SLACK_QA,
                    source_key=key,
                    content_run=None,
                    category=None,
                    status=(
                        KnowledgeItem.Status.AWAITING_ANSWER
                        if waiting
                        else KnowledgeItem.Status.PENDING
                    ),
                    title=_provisional_title(root_question.content),
                    summary=(
                        "답변 대기 중" if waiting else summarize(answer)
                    ),
                    question=question,
                    answer=answer,
                    source_hash=current_hash,
                    generated_at=root_question.generated_at,
                    slack_channel_id=channel_id,
                    slack_thread_ts=thread_ts,
                    slack_source_url=source_url,
                )
                stats["created"] += 1
            else:
                base_updates = {
                    "generated_at": root_question.generated_at,
                    "question": question,
                    "answer": answer,
                    "slack_channel_id": channel_id or existing.slack_channel_id,
                    "slack_thread_ts": thread_ts,
                    "slack_source_url": source_url or existing.slack_source_url,
                }
                source_changed = existing.source_hash != current_hash
                if source_changed:
                    base_updates["verification_status"] = (
                        KnowledgeItem.VerificationStatus.STALE
                        if existing.verified_at
                        else KnowledgeItem.VerificationStatus.UNVERIFIED
                    )
                if source_changed:
                    stats["source_resets"] += 1
                    updates = {
                        **base_updates,
                        "category_id": None,
                        "status": (
                            KnowledgeItem.Status.AWAITING_ANSWER
                            if waiting
                            else KnowledgeItem.Status.PENDING
                        ),
                        "title": _provisional_title(root_question.content),
                        "summary": (
                            "답변 대기 중" if waiting else summarize(answer)
                        ),
                        "source_hash": current_hash,
                        "classification_model": "",
                        "classification_confidence": None,
                        "classification_reason": "",
                        "classified_at": None,
                        "reviewed_by_id": None,
                        "reviewed_at": None,
                    }
                else:
                    updates = base_updates
                if _save_changed(existing, updates):
                    stats["updated"] += 1
                else:
                    stats["unchanged"] += 1

            for message in included_messages:
                message.knowledge_item_id = existing.pk
            desired_keys = [key]
            stats["orphan_messages"] = root_index
        else:
            desired_keys = []
            stats["orphan_messages"] = len(messages)

        if messages:
            FreeQuestionMessage.objects.bulk_update(messages, ["knowledge_item"])
        obsolete = KnowledgeItem.objects.filter(
            source_type=KnowledgeItem.SourceType.SLACK_QA,
            source_key__startswith=f"slack:{thread_ts}:",
        )
        if desired_keys:
            obsolete = obsolete.exclude(source_key__in=desired_keys)
        deleted, _ = obsolete.delete()
        stats["deleted"] = deleted
    return stats
