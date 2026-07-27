import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from django.db import transaction
from django.utils import timezone

from . import llm
from .knowledge_tag_inventory import (
    KnowledgeTagInventoryItem,
    KnowledgeTagInventoryResult,
    collect_locked_knowledge_tag_inventory,
    collect_knowledge_tag_inventory,
)
from .models import (
    KnowledgeItem,
    KnowledgeTag,
    KnowledgeTagActiveSnapshot,
    KnowledgeTagAssignment,
    KnowledgeTagCorpusRevision,
    KnowledgeTagMutationLock,
    KnowledgeTagSnapshot,
)


TAGGING_ARTIFACT_VERSION = "knowledge-tags-v1"
DEFAULT_CANDIDATE_TAGS = ("지식", "참고", "일반")
MAX_INACTIVE_SNAPSHOTS = 2
EXPECTED_CANDIDATE_KEYS = {"tags"}
EXPECTED_ASSIGNMENT_KEYS = {"items"}
TAGGING_MODEL = llm.configured_model_name()
TAGGING_PROVIDER = llm.configured_provider_name()
TAGGING_BATCH_SIZE = 20
TAGGING_TIMEOUT = 240
MAX_TAGGING_OUTPUT_BYTES = 65536


class KnowledgeTaggingError(Exception):
    def __init__(self, code: str, *, summary: dict | None = None):
        self.code = code
        self.summary = summary
        super().__init__(code)


@dataclass(frozen=True)
class TaggingArtifacts:
    directory: Path
    manifest: dict


@dataclass(frozen=True)
class TaggingRunResult:
    summary: dict
    artifacts: TaggingArtifacts
    snapshot: KnowledgeTagSnapshot | None


class TaggingRunner(Protocol):
    def candidate_tags(self, inventory: tuple[KnowledgeTagInventoryItem, ...]) -> list[str]:
        ...

    def assign_tags(
        self,
        inventory: tuple[KnowledgeTagInventoryItem, ...],
        candidates: tuple[str, ...],
    ) -> dict[int, list[str]]:
        ...

    def review_tags(
        self,
        inventory: tuple[KnowledgeTagInventoryItem, ...],
        candidates: tuple[str, ...],
        assignments: dict[int, list[str]],
    ) -> dict[int, list[str]]:
        ...


@dataclass(frozen=True)
class TaggingSource:
    knowledge_item_id: int
    source_key: str
    source_hash: str
    source_type: str
    status: str
    title: str
    category_path: str
    text: str


class DeterministicTaggingRunner:
    def candidate_tags(self, inventory: tuple[KnowledgeTagInventoryItem, ...]) -> list[str]:
        labels = list(DEFAULT_CANDIDATE_TAGS)
        for item in inventory:
            if item.status not in labels:
                labels.append(item.status)
            if item.category_path:
                for segment in item.category_path.split("/"):
                    if segment not in labels:
                        labels.append(segment)
        return labels

    def assign_tags(
        self,
        inventory: tuple[KnowledgeTagInventoryItem, ...],
        candidates: tuple[str, ...],
    ) -> dict[int, list[str]]:
        assignments = {}
        for item in inventory:
            labels = [item.status]
            if item.category_path:
                labels.extend(item.category_path.split("/"))
            labels.extend(candidates)
            assignments[item.knowledge_item_id] = _first_distinct_tags(labels)
        return assignments

    def review_tags(
        self,
        inventory: tuple[KnowledgeTagInventoryItem, ...],
        candidates: tuple[str, ...],
        assignments: dict[int, list[str]],
    ) -> dict[int, list[str]]:
        reviewed = {}
        for item in inventory:
            reviewed[item.knowledge_item_id] = _first_distinct_tags(
                (
                    *(assignments.get(item.knowledge_item_id) or ()),
                    *candidates,
                    *DEFAULT_CANDIDATE_TAGS,
                )
            )
        return reviewed


class LLMTaggingRunner:
    def __init__(
        self,
        *,
        config: llm.LLMConfig,
        timeout: int = TAGGING_TIMEOUT,
        batch_size: int = TAGGING_BATCH_SIZE,
        invoker: Callable[[llm.LLMConfig, dict, int], str] | None = None,
    ):
        self.config = config
        self.timeout = timeout
        self.batch_size = batch_size
        self.invoker = invoker or invoke_llm_tagging

    def candidate_tags(self, inventory: tuple[KnowledgeTagInventoryItem, ...]) -> list[str]:
        candidates: list[str] = []
        seen = set()
        for batch in _batches(inventory, self.batch_size):
            sources = _source_payloads(batch)
            prompt = _candidate_prompt(sources)
            raw_output = self._invoke("candidates", prompt)
            for label in _parse_candidate_response(raw_output):
                identity = KnowledgeTag.normalize_label(label)
                if identity and identity not in seen:
                    candidates.append(label)
                    seen.add(identity)
        return candidates

    def assign_tags(
        self,
        inventory: tuple[KnowledgeTagInventoryItem, ...],
        candidates: tuple[str, ...],
    ) -> dict[int, list[str]]:
        assignments: dict[int, list[str]] = {}
        for batch in _batches(inventory, self.batch_size):
            sources = _source_payloads(batch)
            prompt = _assignment_prompt(sources, candidates)
            raw_output = self._invoke("assignments", prompt)
            assignments.update(
                _parse_item_tags_response(
                    raw_output,
                    expected_ids=[source.knowledge_item_id for source in sources],
                    error_code="tag_generation",
                )
            )
        return assignments

    def review_tags(
        self,
        inventory: tuple[KnowledgeTagInventoryItem, ...],
        candidates: tuple[str, ...],
        assignments: dict[int, list[str]],
    ) -> dict[int, list[str]]:
        reviewed: dict[int, list[str]] = {}
        for batch in _batches(inventory, self.batch_size):
            sources = _source_payloads(batch)
            batch_assignments = {
                source.knowledge_item_id: assignments[source.knowledge_item_id]
                for source in sources
            }
            prompt = _review_prompt(sources, candidates, batch_assignments)
            raw_output = self._invoke("reviewed", prompt)
            reviewed.update(
                _parse_item_tags_response(
                    raw_output,
                    expected_ids=[source.knowledge_item_id for source in sources],
                    error_code="tag_validation",
                )
            )
        return reviewed

    def _invoke(self, pass_name: str, prompt: dict) -> str:
        return self.invoker(self.config, prompt, self.timeout)


def create_default_runner(*, timeout: int = TAGGING_TIMEOUT) -> LLMTaggingRunner:
    try:
        config = llm.resolve_llm_config()
        llm.preflight_llm(config)
    except llm.LLMConfigError as error:
        raise KnowledgeTaggingError("tag_generation") from error
    return LLMTaggingRunner(config=config, timeout=timeout)


def invoke_llm_tagging(config: llm.LLMConfig, prompt: dict, timeout: int) -> str:
    prompt_text = json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))
    try:
        response = llm.complete(config, prompt_text, timeout=timeout)
    except llm.LLMTransportError as error:
        raise KnowledgeTaggingError("tag_generation") from error
    if len(response.text.encode()) > MAX_TAGGING_OUTPUT_BYTES:
        raise KnowledgeTaggingError("tag_generation")
    return response.text


def _batches(items: tuple[KnowledgeTagInventoryItem, ...], batch_size: int):
    if batch_size < 1:
        raise KnowledgeTaggingError("tag_generation")
    for index in range(0, len(items), batch_size):
        yield items[index : index + batch_size]


def _source_payloads(batch: tuple[KnowledgeTagInventoryItem, ...]) -> tuple[TaggingSource, ...]:
    sources = []
    for candidate in batch:
        text = _prompt_source_text(candidate)
        sources.append(
            TaggingSource(
                knowledge_item_id=candidate.knowledge_item_id,
                source_key=candidate.source_key,
                source_hash=candidate.source_hash,
                source_type=candidate.source_type,
                status=candidate.status,
                title=candidate.title,
                category_path=candidate.category_path,
                text=text,
            )
        )
    return tuple(sources)


def _prompt_source_text(candidate: KnowledgeTagInventoryItem) -> str:
    if candidate.source_text.strip():
        return candidate.source_text.strip()
    return "\n".join(
        (
            f"title: {candidate.title}",
            f"source_type: {candidate.source_type}",
            f"status: {candidate.status}",
            f"category_path: {candidate.category_path or '(none)'}",
            f"source_hash: {candidate.source_hash}",
        )
    )


def _source_prompt_payload(source: TaggingSource) -> dict:
    return {
        "knowledge_item_id": source.knowledge_item_id,
        "source_type": source.source_type,
        "status": source.status,
        "title": source.title,
        "category_path": source.category_path,
        "text": source.text,
    }


def _candidate_prompt(sources: tuple[TaggingSource, ...]) -> dict:
    return {
        "pass": "candidate_vocabulary",
        "instruction": (
            "Scan only the provided knowledge items and return useful tag candidates "
            "for this batch. Keep semantically overlapping labels distinct. Return one "
            "strict JSON object only."
        ),
        "items": [_source_prompt_payload(source) for source in sources],
        "response_contract": {"tags": ["exact label strings"]},
    }


def _assignment_prompt(
    sources: tuple[TaggingSource, ...],
    candidates: tuple[str, ...],
) -> dict:
    return {
        "pass": "assignment",
        "instruction": (
            "Assign at least three relevant tags to each provided knowledge item using "
            "exact strings from the candidate vocabulary only. You may return more than "
            "three tags when useful. Do not merge semantically overlapping labels. "
            "Return every provided knowledge_item_id exactly once."
        ),
        "candidates": list(candidates),
        "items": [_source_prompt_payload(source) for source in sources],
        "response_contract": {
            "items": [
                {"knowledge_item_id": "integer id from input", "tags": ["exact labels"]}
            ]
        },
    }


def _review_prompt(
    sources: tuple[TaggingSource, ...],
    candidates: tuple[str, ...],
    assignments: dict[int, list[str]],
) -> dict:
    return {
        "pass": "review",
        "instruction": (
            "Review and improve the assigned tags for each item. You may add tags not "
            "present in the candidate vocabulary when useful, but every item must keep "
            "at least three exact-label tags and all provided IDs must appear exactly once."
        ),
        "candidates": list(candidates),
        "assignments": [
            {"knowledge_item_id": item_id, "tags": tags}
            for item_id, tags in sorted(assignments.items())
        ],
        "items": [_source_prompt_payload(source) for source in sources],
        "response_contract": {
            "items": [
                {"knowledge_item_id": "integer id from input", "tags": ["exact labels"]}
            ]
        },
    }


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise KnowledgeTaggingError("tag_generation")
        result[key] = value
    return result


def _invalid_constant(_value: str):
    raise KnowledgeTaggingError("tag_generation")


def _parse_json_object(raw_output: str, *, error_code: str) -> dict:
    if len(raw_output.encode()) > MAX_TAGGING_OUTPUT_BYTES:
        raise KnowledgeTaggingError(error_code)
    try:
        payload = json.loads(
            raw_output,
            parse_constant=_invalid_constant,
            object_pairs_hook=_strict_object,
        )
    except KnowledgeTaggingError:
        raise KnowledgeTaggingError(error_code)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise KnowledgeTaggingError(error_code) from error
    if not isinstance(payload, dict):
        raise KnowledgeTaggingError(error_code)
    return payload


def _parse_candidate_response(raw_output: str) -> list[str]:
    payload = _parse_json_object(raw_output, error_code="tag_generation")
    if set(payload) != EXPECTED_CANDIDATE_KEYS:
        raise KnowledgeTaggingError("tag_generation")
    return _validate_candidate_tag_list(payload["tags"])


def _validate_candidate_tag_list(labels: list[str] | tuple[str, ...]) -> list[str]:
    if not isinstance(labels, (list, tuple)):
        raise KnowledgeTaggingError("tag_generation")
    normalized = []
    seen = set()
    for label in labels:
        if not isinstance(label, str):
            raise KnowledgeTaggingError("tag_generation")
        try:
            display_label = KnowledgeTag.display_label(label)
            normalized_label = KnowledgeTag.normalize_label(label)
        except TypeError as error:
            raise KnowledgeTaggingError("tag_generation") from error
        if not normalized_label:
            continue
        if any(_is_control(character) for character in display_label):
            raise KnowledgeTaggingError("tag_generation")
        if normalized_label not in seen:
            normalized.append(display_label)
            seen.add(normalized_label)
    return normalized


def _parse_item_tags_response(
    raw_output: str,
    *,
    expected_ids: list[int],
    error_code: str,
) -> dict[int, list[str]]:
    payload = _parse_json_object(raw_output, error_code=error_code)
    if set(payload) != EXPECTED_ASSIGNMENT_KEYS:
        raise KnowledgeTaggingError(error_code)
    raw_items = payload["items"]
    if not isinstance(raw_items, list):
        raise KnowledgeTaggingError(error_code)
    expected = set(expected_ids)
    result = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, dict) or set(raw_item) != {"knowledge_item_id", "tags"}:
            raise KnowledgeTaggingError(error_code)
        item_id = raw_item["knowledge_item_id"]
        if not isinstance(item_id, int) or item_id in result or item_id not in expected:
            raise KnowledgeTaggingError(error_code)
        result[item_id] = _strict_tag_list(raw_item["tags"])
    if set(result) != expected:
        raise KnowledgeTaggingError(error_code)
    return result


def run_tagging_pipeline(
    *,
    artifact_dir: Path,
    dry_run: bool = True,
    publish: bool = False,
    runner: TaggingRunner | None = None,
    inventory_result: KnowledgeTagInventoryResult | None = None,
    operation_run_id: int | None = None,
) -> TaggingRunResult:
    if dry_run and publish:
        raise KnowledgeTaggingError("tag_validation")
    inventory: tuple[KnowledgeTagInventoryItem, ...] = ()
    summary = _tagging_summary(inventory_count=0, dry_run=dry_run)
    try:
        try:
            inventory_result = inventory_result or collect_knowledge_tag_inventory()
        except RuntimeError as error:
            raise KnowledgeTaggingError(
                "tag_inventory",
                summary=_tagging_summary(inventory_count=0, dry_run=dry_run),
            ) from error
        inventory = inventory_result.eligible
        summary = _tagging_summary(inventory_count=len(inventory), dry_run=dry_run)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if runner is None:
            runner = create_default_runner()
        inventory_payload = _inventory_payload(
            inventory_result.inventory_digest,
            inventory_result.corpus_revision,
            inventory,
        )
        inventory_artifact = _write_artifact(artifact_dir, "inventory", inventory_payload)

        candidates = tuple(_validate_tag_list(runner.candidate_tags(inventory)))
        summary = {**summary, "tag_candidates": len(candidates)}
        candidate_payload = {"version": TAGGING_ARTIFACT_VERSION, "tags": list(candidates)}
        candidate_artifact = _write_artifact(artifact_dir, "candidates", candidate_payload)

        assignments = _validate_assignments(
            runner.assign_tags(inventory, candidates),
            inventory=inventory,
            allowed_tags=candidates,
        )
        summary = {
            **summary,
            "tag_assigned_items": len(assignments),
            "tag_assignments": sum(len(tags) for tags in assignments.values()),
        }
        assignment_payload = _assignments_payload(assignments)
        assignment_artifact = _write_artifact(artifact_dir, "assignments", assignment_payload)

        reviewed = _validate_assignments(
            runner.review_tags(inventory, candidates, assignments),
            inventory=inventory,
            allowed_tags=None,
        )
        summary = {
            **summary,
            "tag_reviewed_items": len(reviewed),
            "tag_assignments": sum(len(tags) for tags in reviewed.values()),
        }
        reviewed_payload = _assignments_payload(reviewed)
        reviewed_artifact = _write_artifact(artifact_dir, "reviewed", reviewed_payload)

        validation_payload = _validation_payload(
            inventory_payload,
            candidate_payload,
            assignment_payload,
            reviewed_payload,
        )
        validation_artifact = _write_artifact(artifact_dir, "validation", validation_payload)
        manifest = {
            "version": TAGGING_ARTIFACT_VERSION,
            "inventory": inventory_artifact,
            "candidates": candidate_artifact,
            "assignments": assignment_artifact,
            "reviewed": reviewed_artifact,
            "validation": validation_artifact,
        }
        validate_tagging_artifacts(artifact_dir, manifest)

        if dry_run:
            return TaggingRunResult(
                summary=summary,
                artifacts=TaggingArtifacts(artifact_dir, manifest),
                snapshot=None,
            )
        if not publish:
            raise KnowledgeTaggingError("tag_validation")

        snapshot = publish_tag_snapshot(
            inventory_digest=inventory_result.inventory_digest,
            corpus_revision=inventory_result.corpus_revision,
            manifest=manifest,
            reviewed=reviewed,
            operation_run_id=operation_run_id,
        )
        if snapshot is None:
            summary = {**summary, "tag_stale_inventory": True}
            return TaggingRunResult(
                summary=summary,
                artifacts=TaggingArtifacts(artifact_dir, manifest),
                snapshot=None,
            )
        summary = {**summary, "tag_published": True}
        return TaggingRunResult(
            summary=summary,
            artifacts=TaggingArtifacts(artifact_dir, manifest),
            snapshot=snapshot,
        )
    except KnowledgeTaggingError as error:
        error.summary = _failed_tagging_summary(summary, len(inventory))
        raise
    except Exception as error:
        raise KnowledgeTaggingError(
            "unexpected_error",
            summary=_failed_tagging_summary(summary, len(inventory)),
        ) from error


def _tagging_summary(*, inventory_count: int, dry_run: bool) -> dict:
    return {
        "tag_inventory": inventory_count,
        "tag_candidates": 0,
        "tag_assigned_items": 0,
        "tag_assignments": 0,
        "tag_reviewed_items": 0,
        "tag_failed_items": 0,
        "tag_dry_run": dry_run,
        "tag_published": False,
        "tag_stale_inventory": False,
    }


def _failed_tagging_summary(summary: dict, inventory_count: int) -> dict:
    failed = max(inventory_count - int(summary.get("tag_reviewed_items") or 0), 0)
    return {**summary, "tag_failed_items": failed}


def publish_tag_snapshot(
    *,
    inventory_digest: str,
    corpus_revision: int,
    manifest: dict,
    reviewed: dict[int, list[str]],
    operation_run_id: int | None = None,
) -> KnowledgeTagSnapshot | None:
    with transaction.atomic():
        KnowledgeTagMutationLock.lock()
        items_by_id = {
            item.pk: item
            for item in KnowledgeItem.objects.select_for_update()
            .filter(pk__in=reviewed)
            .order_by("pk")
        }
        if set(items_by_id) != set(reviewed):
            return None
        locked_revision = KnowledgeTagCorpusRevision.fence()
        fresh = collect_locked_knowledge_tag_inventory(locked_revision)
        if (
            fresh.corpus_revision != corpus_revision
            or fresh.inventory_digest != inventory_digest
            or {item.knowledge_item_id for item in fresh.eligible} != set(reviewed)
        ):
            return None
        previous = (
            KnowledgeTagActiveSnapshot.objects.select_related("snapshot")
            .select_for_update()
            .filter(singleton_key=1)
            .first()
        )
        snapshot = KnowledgeTagSnapshot.objects.create(
            status=KnowledgeTagSnapshot.Status.STAGING,
            inventory_digest=inventory_digest,
            artifact_manifest=manifest,
            operation_run_id=operation_run_id,
            item_count=len(reviewed),
            tag_count=len(
                {
                    KnowledgeTag.normalize_label(label)
                    for tags in reviewed.values()
                    for label in tags
                }
            ),
            assignment_count=sum(len(tags) for tags in reviewed.values()),
        )
        assignments = []
        for item_id in sorted(reviewed):
            item = items_by_id[item_id]
            for position, label in enumerate(reviewed[item_id], start=1):
                assignments.append(
                    KnowledgeTagAssignment(
                        snapshot=snapshot,
                        knowledge_item=item,
                        tag=KnowledgeTag.for_label(label),
                        position=position,
                    )
                )
        KnowledgeTagAssignment.objects.bulk_create(assignments)
        now = timezone.now()
        snapshot.status = KnowledgeTagSnapshot.Status.ACTIVE
        snapshot.published_at = now
        snapshot.save(update_fields=["status", "published_at", "updated_at"])
        if previous:
            old_snapshot = previous.snapshot
            previous.snapshot = snapshot
            previous.save(update_fields=["snapshot", "updated_at"])
            if old_snapshot.status != KnowledgeTagSnapshot.Status.INACTIVE:
                old_snapshot.status = KnowledgeTagSnapshot.Status.INACTIVE
                old_snapshot.save(update_fields=["status", "updated_at"])
        else:
            KnowledgeTagActiveSnapshot.objects.create(snapshot=snapshot)
        _cleanup_inactive_snapshots()
    return snapshot


def _cleanup_inactive_snapshots() -> None:
    inactive_ids = list(
        KnowledgeTagSnapshot.objects.filter(status=KnowledgeTagSnapshot.Status.INACTIVE)
        .order_by("-published_at", "-created_at", "-pk")
        .values_list("pk", flat=True)
    )
    stale_ids = inactive_ids[MAX_INACTIVE_SNAPSHOTS:]
    if stale_ids:
        KnowledgeTagSnapshot.objects.filter(pk__in=stale_ids).delete()


def _inventory_payload(
    inventory_digest: str,
    corpus_revision: int,
    inventory: tuple[KnowledgeTagInventoryItem, ...],
) -> dict:
    return {
        "version": TAGGING_ARTIFACT_VERSION,
        "inventory_digest": inventory_digest,
        "corpus_revision": corpus_revision,
        "items": [
            {
                "knowledge_item_id": item.knowledge_item_id,
                "source_key": item.source_key,
                "source_hash": item.source_hash,
                "source_text_hash": item.source_text_hash,
                "source_type": item.source_type,
                "status": item.status,
                "title_hash": _digest(item.title),
                "category_path": item.category_path,
            }
            for item in inventory
        ],
    }


def _assignments_payload(assignments: dict[int, list[str]]) -> dict:
    return {
        "version": TAGGING_ARTIFACT_VERSION,
        "items": [
            {"knowledge_item_id": item_id, "tags": tags}
            for item_id, tags in sorted(assignments.items())
        ],
    }


def _validation_payload(
    inventory_payload: dict,
    candidate_payload: dict,
    assignment_payload: dict,
    reviewed_payload: dict,
) -> dict:
    candidate_count = len(candidate_payload["tags"])
    reviewed_items = reviewed_payload["items"]
    return {
        "version": TAGGING_ARTIFACT_VERSION,
        "inventory_digest": inventory_payload["inventory_digest"],
        "corpus_revision": inventory_payload["corpus_revision"],
        "inventory_artifact_digest": _payload_digest(inventory_payload),
        "candidate_digest": _payload_digest(candidate_payload),
        "assignment_digest": _payload_digest(assignment_payload),
        "reviewed_digest": _payload_digest(reviewed_payload),
        "inventory_count": len(inventory_payload["items"]),
        "candidate_count": candidate_count,
        "assigned_count": len(assignment_payload["items"]),
        "reviewed_count": len(reviewed_items),
        "assignment_count": sum(len(item["tags"]) for item in reviewed_items),
        "min_tags_per_item": 3,
        "valid": True,
    }


def _write_artifact(artifact_dir: Path, name: str, payload: dict) -> dict:
    path = artifact_dir / f"{name}.json"
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    content = serialized + "\n"
    path.write_text(content, encoding="utf-8")
    return {
        "path": path.name,
        "sha256": _digest(content),
        "bytes": len(content.encode()),
    }


def validate_tagging_artifacts(artifact_dir: Path, manifest: dict) -> None:
    expected_payload_keys = {
        "inventory": {"version", "inventory_digest", "corpus_revision", "items"},
        "candidates": {"version", *EXPECTED_CANDIDATE_KEYS},
        "assignments": {"version", *EXPECTED_ASSIGNMENT_KEYS},
        "reviewed": {"version", *EXPECTED_ASSIGNMENT_KEYS},
        "validation": {
            "version",
            "inventory_digest",
            "corpus_revision",
            "inventory_artifact_digest",
            "candidate_digest",
            "assignment_digest",
            "reviewed_digest",
            "inventory_count",
            "candidate_count",
            "assigned_count",
            "reviewed_count",
            "assignment_count",
            "min_tags_per_item",
            "valid",
        },
    }
    payloads = {}
    for key, required_keys in expected_payload_keys.items():
        artifact = manifest.get(key)
        if not isinstance(artifact, dict):
            raise KnowledgeTaggingError("tag_artifact")
        path = artifact_dir / str(artifact.get("path", ""))
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as error:
            raise KnowledgeTaggingError("tag_artifact") from error
        if _digest(content) != artifact.get("sha256"):
            raise KnowledgeTaggingError("tag_artifact")
        try:
            payload = json.loads(
                content,
                parse_constant=_invalid_artifact_constant,
                object_pairs_hook=_strict_artifact_object,
            )
        except KnowledgeTaggingError:
            raise
        except json.JSONDecodeError as error:
            raise KnowledgeTaggingError("tag_artifact") from error
        if not isinstance(payload, dict) or set(payload) != required_keys:
            raise KnowledgeTaggingError("tag_artifact")
        if payload["version"] != TAGGING_ARTIFACT_VERSION:
            raise KnowledgeTaggingError("tag_artifact")
        payloads[key] = payload
    _validate_artifact_cross_references(payloads)


def _validate_artifact_cross_references(payloads: dict[str, dict]) -> None:
    inventory = payloads["inventory"]
    candidates = payloads["candidates"]
    assignments = payloads["assignments"]
    reviewed = payloads["reviewed"]
    validation = payloads["validation"]
    inventory_items = inventory["items"]
    assignment_items = assignments["items"]
    reviewed_items = reviewed["items"]
    if (
        validation["inventory_digest"] != inventory["inventory_digest"]
        or validation["corpus_revision"] != inventory["corpus_revision"]
        or validation["inventory_artifact_digest"] != _payload_digest(inventory)
        or validation["candidate_digest"] != _payload_digest(candidates)
        or validation["assignment_digest"] != _payload_digest(assignments)
        or validation["reviewed_digest"] != _payload_digest(reviewed)
    ):
        raise KnowledgeTaggingError("tag_artifact")
    inventory_ids = _artifact_ids(inventory_items)
    assignment_ids = _artifact_ids(assignment_items)
    reviewed_ids = _artifact_ids(reviewed_items)
    if inventory_ids != assignment_ids or inventory_ids != reviewed_ids:
        raise KnowledgeTaggingError("tag_artifact")
    if (
        validation["inventory_count"] != len(inventory_items)
        or validation["candidate_count"] != len(candidates["tags"])
        or validation["assigned_count"] != len(assignment_items)
        or validation["reviewed_count"] != len(reviewed_items)
        or validation["assignment_count"] != sum(len(item["tags"]) for item in reviewed_items)
        or validation["min_tags_per_item"] != 3
        or validation["valid"] is not True
    ):
        raise KnowledgeTaggingError("tag_artifact")
    _validate_artifact_labels(candidates["tags"], require_minimum=False)
    candidate_set = {KnowledgeTag.normalize_label(label) for label in candidates["tags"]}
    for item in assignment_items:
        if not isinstance(item, dict) or set(item) != {"knowledge_item_id", "tags"}:
            raise KnowledgeTaggingError("tag_artifact")
        _validate_artifact_labels(item["tags"], require_minimum=True)
        identities = {KnowledgeTag.normalize_label(label) for label in item["tags"]}
        if not identities.issubset(candidate_set):
            raise KnowledgeTaggingError("tag_artifact")
    for item in reviewed_items:
        if not isinstance(item, dict) or set(item) != {"knowledge_item_id", "tags"}:
            raise KnowledgeTaggingError("tag_artifact")
        _validate_artifact_labels(item["tags"], require_minimum=True)


def _artifact_ids(items: list[dict]) -> set[int]:
    if not isinstance(items, list):
        raise KnowledgeTaggingError("tag_artifact")
    ids = set()
    for item in items:
        if not isinstance(item, dict) or "knowledge_item_id" not in item:
            raise KnowledgeTaggingError("tag_artifact")
        item_id = item["knowledge_item_id"]
        if not isinstance(item_id, int) or item_id in ids:
            raise KnowledgeTaggingError("tag_artifact")
        ids.add(item_id)
    return ids


def _validate_artifact_labels(labels, *, require_minimum: bool) -> None:
    if not isinstance(labels, list):
        raise KnowledgeTaggingError("tag_artifact")
    normalized = [
        KnowledgeTag.normalize_label(label)
        for label in labels
        if isinstance(label, str) and KnowledgeTag.normalize_label(label)
    ]
    if len(normalized) != len(set(normalized)):
        raise KnowledgeTaggingError("tag_artifact")
    _validate_tag_list(labels)
    if require_minimum and len(normalized) < 3:
        raise KnowledgeTaggingError("tag_artifact")


def _strict_artifact_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise KnowledgeTaggingError("tag_artifact")
        result[key] = value
    return result


def _invalid_artifact_constant(_value: str):
    raise KnowledgeTaggingError("tag_artifact")


def _validate_tag_list(labels: list[str] | tuple[str, ...]) -> list[str]:
    if not isinstance(labels, (list, tuple)):
        raise KnowledgeTaggingError("tag_generation")
    normalized = []
    seen = set()
    for label in labels:
        if not isinstance(label, str):
            raise KnowledgeTaggingError("tag_generation")
        try:
            display_label = KnowledgeTag.display_label(label)
            normalized_label = KnowledgeTag.normalize_label(label)
        except TypeError as error:
            raise KnowledgeTaggingError("tag_generation") from error
        if not normalized_label:
            continue
        if any(_is_control(character) for character in display_label):
            raise KnowledgeTaggingError("tag_generation")
        if normalized_label not in seen:
            normalized.append(display_label)
            seen.add(normalized_label)
    if len(normalized) < 3:
        normalized = _first_distinct_tags((*normalized, *DEFAULT_CANDIDATE_TAGS))
    return normalized


def _strict_tag_list(labels: list[str] | tuple[str, ...]) -> list[str]:
    normalized = _validate_tag_list(labels)
    source_count = len(
        {
            KnowledgeTag.normalize_label(label)
            for label in labels
            if isinstance(label, str) and KnowledgeTag.normalize_label(label)
        }
    )
    if source_count < 3:
        raise KnowledgeTaggingError("tag_validation")
    return normalized


def _validate_assignments(
    assignments: dict[int, list[str]],
    *,
    inventory: tuple[KnowledgeTagInventoryItem, ...],
    allowed_tags: tuple[str, ...] | None,
) -> dict[int, list[str]]:
    if not isinstance(assignments, dict):
        raise KnowledgeTaggingError("tag_validation")
    expected_ids = {item.knowledge_item_id for item in inventory}
    if set(assignments) != expected_ids:
        raise KnowledgeTaggingError("tag_validation")
    result = {}
    allowed = {}
    if allowed_tags is not None:
        for label in allowed_tags:
            normalized = KnowledgeTag.normalize_label(label)
            if normalized and normalized not in allowed:
                allowed[normalized] = KnowledgeTag.display_label(label)
    for item_id, labels in assignments.items():
        if allowed_tags is not None:
            normalized_labels = _strict_tag_list(labels)
            projected = []
            seen = set()
            for label in normalized_labels:
                normalized = KnowledgeTag.normalize_label(label)
                if normalized in allowed and normalized not in seen:
                    projected.append(allowed[normalized])
                    seen.add(normalized)
            if len(projected) < 3:
                raise KnowledgeTaggingError("tag_validation")
            result[item_id] = projected
            continue

        normalized = _strict_tag_list(labels)
        if len(normalized) < 3:
            raise KnowledgeTaggingError("tag_validation")
        result[item_id] = normalized
    return result


def _first_distinct_tags(labels) -> list[str]:
    result = []
    seen = set()
    for label in labels:
        if not isinstance(label, str):
            continue
        display_label = KnowledgeTag.display_label(label)
        normalized = KnowledgeTag.normalize_label(label)
        if normalized and normalized not in seen:
            result.append(display_label)
            seen.add(normalized)
        if len(result) >= 3:
            break
    return result


def _is_control(character: str) -> bool:
    import unicodedata

    return unicodedata.category(character) == "Cc"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _payload_digest(payload: dict) -> str:
    return _digest(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
