import hashlib
from dataclasses import dataclass
from typing import Iterable

from django.db.models import Q

from .models import Category, ContentRun, KnowledgeItem, QuizDomainConfig

@dataclass(frozen=True)
class QuizInventoryCandidate:
    knowledge_item_id: int
    domain: str
    source_key: str
    source_hash: str
    title: str
    category_path: str
    allowed_question_types: tuple[str, ...]


@dataclass(frozen=True)
class QuizInventoryQuarantine:
    knowledge_item_id: int
    source_key: str
    source_hash: str
    title: str
    reason: str


@dataclass(frozen=True)
class QuizInventoryResult:
    inventory_version: str
    eligible: tuple[QuizInventoryCandidate, ...]
    quarantined: tuple[QuizInventoryQuarantine, ...]


def collect_quiz_inventory(
    *,
    aws_allowlisted_external_ids: Iterable[str] = (),
    aws_allowlisted_source_keys: Iterable[str] = (),
) -> QuizInventoryResult:
    allowlisted_external_ids = frozenset(aws_allowlisted_external_ids)
    allowlisted_source_keys = frozenset(aws_allowlisted_source_keys)
    domain_configs = {
        Category.canonical_path_key(config.category_path): config
        for config in QuizDomainConfig.objects.filter(enabled=True)
    }
    queryset = (
        KnowledgeItem.objects.select_related("category", "content_run", "content_run__job")
        .filter(
            status=KnowledgeItem.Status.CLASSIFIED,
            hidden_at__isnull=True,
            category__path_key__in=domain_configs,
        )
        .filter(Q(consumption_state__isnull=True) | Q(consumption_state__archived_at__isnull=True))
        .order_by("category__path_key", "source_key", "pk")
    )
    eligible: list[QuizInventoryCandidate] = []
    quarantined: list[QuizInventoryQuarantine] = []
    for item in queryset:
        source_text = _source_text(item)
        if not source_text:
            continue
        config = domain_configs[item.category.path_key]
        domain = config.slug
        if config.requires_allowlist:
            reason = _allowlist_quarantine_reason(
                item,
                domain=domain,
                allowlisted_external_ids=allowlisted_external_ids,
                allowlisted_source_keys=allowlisted_source_keys,
            )
            if reason:
                quarantined.append(_quarantine(item, reason))
                continue
        eligible.append(
            QuizInventoryCandidate(
                knowledge_item_id=item.pk,
                domain=domain,
                source_key=item.source_key,
                source_hash=item.source_hash,
                title=item.title,
                category_path=item.category.path,
                allowed_question_types=tuple(config.allowed_question_types),
            )
        )
    return QuizInventoryResult(
        inventory_version=_inventory_version(eligible, quarantined),
        eligible=tuple(eligible),
        quarantined=tuple(quarantined),
    )


def _source_text(item: KnowledgeItem) -> str:
    if item.source_type == KnowledgeItem.SourceType.CRON:
        run = item.content_run
        if (
            run is None
            or run.status != ContentRun.Status.SUCCESS
            or run.hidden_at is not None
        ):
            return ""
        return (run.body or run.raw_text or "").strip()
    if item.source_type == KnowledgeItem.SourceType.SLACK_QA:
        return (item.answer or "").strip()
    return ""


def _allowlist_quarantine_reason(
    item: KnowledgeItem,
    *,
    domain: str,
    allowlisted_external_ids: frozenset[str],
    allowlisted_source_keys: frozenset[str],
) -> str:
    reason_prefix = "aws" if domain == "aws_saa" else domain
    if item.source_type != KnowledgeItem.SourceType.CRON:
        return f"{reason_prefix}_slack_qa_requires_explicit_review"
    external_id = item.content_run.job.external_id if item.content_run and item.content_run.job else ""
    if item.source_key in allowlisted_source_keys or external_id in allowlisted_external_ids:
        return ""
    return f"{reason_prefix}_cron_not_allowlisted"


def _quarantine(item: KnowledgeItem, reason: str) -> QuizInventoryQuarantine:
    return QuizInventoryQuarantine(
        knowledge_item_id=item.pk,
        source_key=item.source_key,
        source_hash=item.source_hash,
        title=item.title,
        reason=reason,
    )


def _inventory_version(
    eligible: list[QuizInventoryCandidate],
    quarantined: list[QuizInventoryQuarantine],
) -> str:
    digest = hashlib.sha256()
    for candidate in eligible:
        value = "\0".join(
            (
                "eligible",
                str(candidate.knowledge_item_id),
                candidate.source_key,
                candidate.source_hash,
                candidate.domain,
                ",".join(candidate.allowed_question_types),
            )
        )
        digest.update(f"{value}\n".encode())
    for candidate in quarantined:
        value = "\0".join(
            (
                "quarantine",
                str(candidate.knowledge_item_id),
                candidate.source_key,
                candidate.source_hash,
                candidate.reason,
            )
        )
        digest.update(f"{value}\n".encode())
    return digest.hexdigest()
