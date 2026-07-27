import hashlib
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q

from .models import Category, KnowledgeItem, KnowledgeTagCorpusRevision
from .quiz_inventory import AWS_PATH, ENGLISH_PATH, JAPANESE_PATH


EXCLUDED_LEARNING_PATHS = (ENGLISH_PATH, JAPANESE_PATH, AWS_PATH)


@dataclass(frozen=True)
class KnowledgeTagInventoryItem:
    knowledge_item_id: int
    source_key: str
    source_hash: str
    source_text_hash: str
    source_type: str
    status: str
    title: str
    category_path: str
    source_text: str = ""


@dataclass(frozen=True)
class KnowledgeTagInventoryResult:
    inventory_digest: str
    corpus_revision: int
    eligible: tuple[KnowledgeTagInventoryItem, ...]


def collect_knowledge_tag_inventory(*, max_attempts: int = 3) -> KnowledgeTagInventoryResult:
    for _attempt in range(max_attempts):
        before = KnowledgeTagCorpusRevision.get_current().revision
        eligible = _collect_inventory_items()
        after = KnowledgeTagCorpusRevision.get_current().revision
        if before == after:
            return KnowledgeTagInventoryResult(
                inventory_digest=_inventory_digest(after, eligible),
                corpus_revision=after,
                eligible=eligible,
            )
    raise RuntimeError("knowledge tag inventory changed during collection")


def collect_locked_knowledge_tag_inventory(
    locked_revision: KnowledgeTagCorpusRevision,
) -> KnowledgeTagInventoryResult:
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("locked inventory collection requires an active transaction")
    eligible = _collect_inventory_items()
    locked_revision.refresh_from_db(fields=["revision"])
    return KnowledgeTagInventoryResult(
        inventory_digest=_inventory_digest(locked_revision.revision, eligible),
        corpus_revision=locked_revision.revision,
        eligible=eligible,
    )


def _collect_inventory_items() -> tuple[KnowledgeTagInventoryItem, ...]:
    excluded_path_keys = {
        Category.canonical_path_key(path) for path in EXCLUDED_LEARNING_PATHS
    }
    queryset = (
        KnowledgeItem.objects.select_related("category", "content_run")
        .filter(hidden_at__isnull=True)
        .filter(Q(content_run__isnull=True) | Q(content_run__hidden_at__isnull=True))
        .exclude(category__path_key__in=excluded_path_keys)
        .order_by("source_key", "pk")
    )
    items = []
    for item in queryset:
        source_text = _source_text(item)
        items.append(
            KnowledgeTagInventoryItem(
                knowledge_item_id=item.pk,
                source_key=item.source_key,
                source_hash=item.source_hash,
                source_text_hash=hashlib.sha256(source_text.encode()).hexdigest(),
                source_type=item.source_type,
                status=item.status,
                title=item.title,
                category_path=item.category.path if item.category_id else "",
                source_text=source_text,
            )
        )
    return tuple(items)


def _inventory_digest(corpus_revision: int, eligible: tuple[KnowledgeTagInventoryItem, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(f"revision:{corpus_revision}\n".encode())
    for item in eligible:
        title_hash = hashlib.sha256(item.title.encode()).hexdigest()
        value = "\0".join(
            (
                str(item.knowledge_item_id),
                item.source_key,
                item.source_hash,
                item.source_text_hash,
                item.source_type,
                item.status,
                title_hash,
                item.category_path,
            )
        )
        digest.update(f"{value}\n".encode())
    return digest.hexdigest()


def _source_text(item: KnowledgeItem) -> str:
    if item.source_type == KnowledgeItem.SourceType.CRON:
        run = item.content_run
        if run is None or run.hidden_at is not None:
            return ""
        return (run.body or run.raw_text or "").strip()
    if item.source_type == KnowledgeItem.SourceType.SLACK_QA:
        return "\n\n".join(
            value
            for value in (item.question.strip(), item.answer.strip())
            if value
        ).strip()
    return ""
