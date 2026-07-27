import json
import hashlib
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from . import llm
from .knowledge_tag_inventory import collect_knowledge_tag_inventory
from .knowledge_tagging import (
    LLMTaggingRunner,
    KnowledgeTaggingError,
    TaggingSource,
    _assignment_prompt,
    _payload_digest,
    _write_artifact,
    publish_tag_snapshot,
    run_tagging_pipeline,
    validate_tagging_artifacts,
)
from .models import (
    Category,
    ContentRun,
    CronJob,
    KnowledgeItem,
    KnowledgeTagActiveSnapshot,
    KnowledgeTagAssignment,
    KnowledgeTagSnapshot,
    OperationRun,
)


NOW = datetime(2026, 7, 22, tzinfo=UTC)
FAKE_LLM_CONFIG = llm.LLMConfig(
    provider="anthropic",
    model="test-model",
    api_key="test-key",
    max_tokens=100,
)


def create_category_path(path: str) -> Category:
    parent = None
    pieces = []
    category = None
    for depth, name in enumerate(path.split("/"), start=1):
        pieces.append(name)
        category, _ = Category.objects.get_or_create(
            path="/".join(pieces),
            defaults={"name": name, "parent": parent, "depth": depth},
        )
        parent = category
    return category


class RecordingRunner:
    def __init__(self, *, fail_review=False):
        self.calls = []
        self.fail_review = fail_review

    def candidate_tags(self, inventory):
        self.calls.append(("candidates", tuple(item.knowledge_item_id for item in inventory)))
        return ["보안", "Security", "AWS 보안", "운영"]

    def assign_tags(self, inventory, candidates):
        self.calls.append(("assignments", candidates))
        return {
            item.knowledge_item_id: ["보안", "Security", "AWS 보안"]
            for item in inventory
        }

    def review_tags(self, inventory, candidates, assignments):
        self.calls.append(("reviewed", tuple(sorted(assignments))))
        if self.fail_review:
            return {
                item.knowledge_item_id: ["보안", "Security"]
                for item in inventory
            }
        return {
            item.knowledge_item_id: [
                *assignments[item.knowledge_item_id],
                "운영",
            ]
            for item in inventory
        }


class OutOfCandidateAssignmentRunner(RecordingRunner):
    def assign_tags(self, inventory, candidates):
        return {
            item.knowledge_item_id: ["보안", "Security", "후보밖"]
            for item in inventory
        }


class ProjectedAssignmentRunner(RecordingRunner):
    def assign_tags(self, inventory, candidates):
        return {
            item.knowledge_item_id: ["security", "후보밖", "AWS 보안", "운영", "보안"]
            for item in inventory
        }


class ReviewAddsOutsideCandidateRunner(RecordingRunner):
    def review_tags(self, inventory, candidates, assignments):
        return {
            item.knowledge_item_id: [
                *assignments[item.knowledge_item_id],
                "후보밖",
            ]
            for item in inventory
        }


class CallbackReviewRunner(RecordingRunner):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def review_tags(self, inventory, candidates, assignments):
        reviewed = super().review_tags(inventory, candidates, assignments)
        self.callback()
        return reviewed


class FailingCandidateRunner(RecordingRunner):
    def candidate_tags(self, inventory):
        raise KnowledgeTaggingError("tag_generation")


def fake_llm_invoker(config, prompt, timeout):
    if prompt["pass"] == "candidate_vocabulary":
        return json.dumps({"tags": ["보안", "Security", "AWS 보안", "운영"]}, ensure_ascii=False)
    return json.dumps(
        {
            "items": [
                {
                    "knowledge_item_id": item["knowledge_item_id"],
                    "tags": ["보안", "Security", "AWS 보안"],
                }
                for item in prompt["items"]
            ]
        },
        ensure_ascii=False,
    )


class KnowledgeTaggingGenerationTests(TestCase):
    def setUp(self):
        self.lock_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.lock_directory.cleanup)
        self.settings_override = override_settings(
            TAG_KNOWLEDGE_LOCK_PATH=str(Path(self.lock_directory.name) / "tag_knowledge.lock")
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.ops = create_category_path("업무/운영/장애")
        self.english = create_category_path("학습/언어/영어")
        self.first = self.create_item("first", self.ops, "a" * 64)
        self.second = self.create_item("second", self.ops, "b" * 64)
        self.create_item("english", self.english, "c" * 64)
        self.initial_snapshot_id = KnowledgeTagActiveSnapshot.objects.get().snapshot_id

    def create_item(self, external_id: str, category: Category, source_hash: str):
        job = CronJob.objects.create(external_id=external_id, name=external_id)
        run = ContentRun.objects.create(
            job=job,
            status=ContentRun.Status.SUCCESS,
            title=f"{external_id} title",
            body=f"{external_id} body",
            generated_at=NOW,
        )
        return KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.CRON,
            source_key=f"cron:{run.pk}",
            content_run=run,
            category=category,
            status=KnowledgeItem.Status.CLASSIFIED,
            title=f"{external_id} title",
            summary="summary",
            source_hash=source_hash,
            generated_at=NOW,
            classified_at=NOW,
        )

    def test_dry_run_writes_replayable_artifacts_without_pointer_swap(self):
        runner = RecordingRunner()
        with tempfile.TemporaryDirectory() as directory:
            result = run_tagging_pipeline(
                artifact_dir=Path(directory),
                dry_run=True,
                publish=False,
                runner=runner,
            )

            self.assertEqual(
                [call[0] for call in runner.calls],
                ["candidates", "assignments", "reviewed"],
            )
            self.assertFalse(result.summary["tag_published"])
            self.assertEqual(result.summary["tag_inventory"], 2)
            self.assertEqual(
                KnowledgeTagActiveSnapshot.objects.get().snapshot_id,
                self.initial_snapshot_id,
            )
            for artifact in (
                result.artifacts.manifest[key]
                for key in ("inventory", "candidates", "assignments", "reviewed", "validation")
            ):
                content = (Path(directory) / artifact["path"]).read_text()
                self.assertEqual(len(artifact["sha256"]), 64)
                self.assertTrue(content.endswith("\n"))
            inventory = json.loads((Path(directory) / "inventory.json").read_text())
            self.assertNotIn("body", json.dumps(inventory, ensure_ascii=False))
            self.assertIn("title_hash", inventory["items"][0])
            for artifact_name in ("inventory", "candidates", "assignments", "reviewed", "validation"):
                artifact_text = (Path(directory) / f"{artifact_name}.json").read_text()
                self.assertNotIn("first body", artifact_text)
                self.assertNotIn("second body", artifact_text)

            (Path(directory) / "validation.json").write_text("corrupted\n")
            with self.assertRaises(KnowledgeTaggingError):
                validate_tagging_artifacts(Path(directory), result.artifacts.manifest)

    def test_artifact_assignment_candidate_cross_reference_uses_normalized_identity(self):
        inventory = collect_knowledge_tag_inventory()
        inventory_payload = {
            "version": "knowledge-tags-v1",
            "inventory_digest": inventory.inventory_digest,
            "corpus_revision": inventory.corpus_revision,
            "items": [
                {
                    "knowledge_item_id": item.knowledge_item_id,
                    "source_key": item.source_key,
                    "source_hash": item.source_hash,
                    "source_text_hash": item.source_text_hash,
                    "source_type": item.source_type,
                    "status": item.status,
                    "title_hash": hashlib.sha256(item.title.encode()).hexdigest(),
                    "category_path": item.category_path,
                }
                for item in inventory.eligible
            ],
        }
        candidates_payload = {
            "version": "knowledge-tags-v1",
            "tags": ["Security", "보안", "AWS 보안"],
        }
        assignments_payload = {
            "version": "knowledge-tags-v1",
            "items": [
                {
                    "knowledge_item_id": item.knowledge_item_id,
                    "tags": ["security", "보안", "AWS 보안"],
                }
                for item in inventory.eligible
            ],
        }
        validation_payload = {
            "version": "knowledge-tags-v1",
            "inventory_digest": inventory_payload["inventory_digest"],
            "corpus_revision": inventory_payload["corpus_revision"],
            "inventory_artifact_digest": "",
            "candidate_digest": "",
            "assignment_digest": "",
            "reviewed_digest": "",
            "inventory_count": len(inventory.eligible),
            "candidate_count": 3,
            "assigned_count": len(inventory.eligible),
            "reviewed_count": len(inventory.eligible),
            "assignment_count": len(inventory.eligible) * 3,
            "min_tags_per_item": 3,
            "valid": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            inventory_artifact = _write_artifact(artifact_dir, "inventory", inventory_payload)
            candidate_artifact = _write_artifact(artifact_dir, "candidates", candidates_payload)
            assignment_artifact = _write_artifact(artifact_dir, "assignments", assignments_payload)
            reviewed_artifact = _write_artifact(artifact_dir, "reviewed", assignments_payload)
            validation_payload.update(
                {
                    "inventory_artifact_digest": _payload_digest(inventory_payload),
                    "candidate_digest": _payload_digest(candidates_payload),
                    "assignment_digest": _payload_digest(assignments_payload),
                    "reviewed_digest": _payload_digest(assignments_payload),
                }
            )
            validation_artifact = _write_artifact(artifact_dir, "validation", validation_payload)
            manifest = {
                "version": "knowledge-tags-v1",
                "inventory": inventory_artifact,
                "candidates": candidate_artifact,
                "assignments": assignment_artifact,
                "reviewed": reviewed_artifact,
                "validation": validation_artifact,
            }
            validate_tagging_artifacts(artifact_dir, manifest)

            assignments_payload["items"][0]["tags"] = ["outside", "보안", "AWS 보안"]
            manifest["assignments"] = _write_artifact(
                artifact_dir,
                "assignments",
                assignments_payload,
            )
            with self.assertRaises(KnowledgeTaggingError):
                validate_tagging_artifacts(artifact_dir, manifest)

    def test_publish_swaps_pointer_after_validation_and_preserves_semantic_tags(self):
        runner = RecordingRunner()
        with tempfile.TemporaryDirectory() as directory:
            result = run_tagging_pipeline(
                artifact_dir=Path(directory),
                dry_run=False,
                publish=True,
                runner=runner,
            )

        active_snapshot = KnowledgeTagActiveSnapshot.objects.get().snapshot
        self.assertEqual(result.snapshot, active_snapshot)
        self.assertNotEqual(active_snapshot.pk, self.initial_snapshot_id)
        self.assertTrue(result.summary["tag_published"])
        self.assertEqual(active_snapshot.item_count, 2)
        self.assertEqual(active_snapshot.assignment_count, 8)
        self.assertEqual(
            set(
                KnowledgeTagAssignment.objects.filter(
                    snapshot=active_snapshot,
                    knowledge_item=self.first,
                ).values_list("tag__label", flat=True)
            ),
            {"보안", "Security", "AWS 보안", "운영"},
        )
        self.assertEqual(
            KnowledgeTagSnapshot.objects.get(pk=self.initial_snapshot_id).status,
            KnowledgeTagSnapshot.Status.INACTIVE,
        )

    def test_failed_validation_keeps_previous_active_snapshot(self):
        runner = RecordingRunner(fail_review=True)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(KnowledgeTaggingError):
            run_tagging_pipeline(
                artifact_dir=Path(directory),
                dry_run=False,
                publish=True,
                runner=runner,
            )

        self.assertEqual(
            KnowledgeTagActiveSnapshot.objects.get().snapshot_id,
            self.initial_snapshot_id,
        )
        self.assertEqual(KnowledgeTagSnapshot.objects.count(), 1)

    def test_assignment_projects_candidate_vocabulary_and_review_can_add_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            run_tagging_pipeline(
                artifact_dir=Path(directory),
                dry_run=True,
                publish=False,
                runner=ProjectedAssignmentRunner(),
            )
            assignments = json.loads((Path(directory) / "assignments.json").read_text())

        self.assertEqual(
            assignments["items"][0]["tags"],
            ["Security", "AWS 보안", "운영", "보안"],
        )

        with tempfile.TemporaryDirectory() as directory, self.assertRaises(KnowledgeTaggingError):
            run_tagging_pipeline(
                artifact_dir=Path(directory),
                dry_run=True,
                publish=False,
                runner=OutOfCandidateAssignmentRunner(),
            )

        with tempfile.TemporaryDirectory() as directory:
            result = run_tagging_pipeline(
                artifact_dir=Path(directory),
                dry_run=True,
                publish=False,
                runner=ReviewAddsOutsideCandidateRunner(),
            )
            reviewed = json.loads((Path(directory) / "reviewed.json").read_text())

        self.assertFalse(result.summary["tag_published"])
        self.assertIn("후보밖", reviewed["items"][0]["tags"])

    def test_assignment_prompt_requires_candidates_without_three_tag_cap(self):
        prompt = _assignment_prompt(
            (
                TaggingSource(
                    knowledge_item_id=1,
                    source_key="cron:1",
                    source_hash="hash",
                    source_type="cron",
                    status="classified",
                    title="title",
                    category_path="업무/운영",
                    text="text",
                ),
            ),
            ("보안", "Security", "AWS 보안", "운영"),
        )
        instruction = prompt["instruction"].casefold()

        self.assertIn("exact strings from the candidate vocabulary only", instruction)
        self.assertIn("at least three", instruction)
        self.assertIn("more than three", instruction)
        self.assertNotIn("exactly three", instruction)

    def test_empty_authoritative_source_text_uses_frozen_metadata_fallback(self):
        self.first.content_run.body = ""
        self.first.content_run.raw_text = ""
        self.first.content_run.save(update_fields=["body", "raw_text", "updated_at"])
        prompts = []

        def invoker(config, prompt, timeout):
            prompts.append(prompt)
            return fake_llm_invoker(config, prompt, timeout)

        with tempfile.TemporaryDirectory() as directory:
            runner = LLMTaggingRunner(
                config=FAKE_LLM_CONFIG,
                invoker=invoker,
            )
            result = run_tagging_pipeline(
                artifact_dir=Path(directory),
                dry_run=True,
                publish=False,
                runner=runner,
            )

        self.assertEqual(result.summary["tag_inventory"], 2)
        first_prompt_item = next(
            item
            for item in prompts[0]["items"]
            if item["knowledge_item_id"] == self.first.pk
        )
        self.assertIn("title: first title", first_prompt_item["text"])
        self.assertIn("source_hash:", first_prompt_item["text"])

    def test_stale_inventory_aborts_without_pointer_swap(self):
        runner = CallbackReviewRunner(
            lambda: self.create_item("stale-direct", self.ops, "f" * 64)
        )
        with tempfile.TemporaryDirectory() as directory:
            result = run_tagging_pipeline(
                artifact_dir=Path(directory),
                dry_run=False,
                publish=True,
                runner=runner,
            )

        self.assertTrue(result.summary["tag_stale_inventory"])
        self.assertFalse(result.summary["tag_published"])
        self.assertIsNone(result.snapshot)
        self.assertEqual(
            KnowledgeTagActiveSnapshot.objects.get().snapshot_id,
            self.initial_snapshot_id,
        )

    def test_command_records_tagging_operation_and_can_publish(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "dashboard.management.commands.tag_knowledge.knowledge_tagging.create_default_runner",
            return_value=RecordingRunner(),
        ):
            call_command("tag_knowledge", "--publish", "--artifact-dir", directory)

        run = OperationRun.objects.latest("id")
        self.assertEqual(run.kind, OperationRun.Kind.TAGGING)
        self.assertEqual(run.status, OperationRun.Status.SUCCESS)
        self.assertTrue(run.summary["tag_published"])
        self.assertEqual(run.summary["tag_inventory"], 2)
        manifest = KnowledgeTagActiveSnapshot.objects.get().snapshot.artifact_manifest
        self.assertEqual(len(manifest["validation"]["sha256"]), 64)
        self.assertEqual(KnowledgeTagActiveSnapshot.objects.get().snapshot.operation_run, run)

    def test_command_preflight_failure_preserves_inventory_target_summary(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "dashboard.management.commands.tag_knowledge.knowledge_tagging.create_default_runner",
            side_effect=KnowledgeTaggingError("tag_generation"),
        ):
            with self.assertRaises(Exception):
                call_command("tag_knowledge", "--artifact-dir", directory)

        run = OperationRun.objects.latest("id")
        self.assertEqual(run.status, OperationRun.Status.FAILED)
        self.assertEqual(run.error_code, "tag_generation")
        self.assertEqual(run.summary["tag_inventory"], 2)
        self.assertEqual(run.summary["tag_failed_items"], 2)
        self.assertTrue(run.summary["tag_dry_run"])
        self.assertFalse(run.summary["tag_published"])

    def test_command_llm_initialization_failure_preserves_inventory_target_summary(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "dashboard.llm.resolve_llm_config",
            side_effect=llm.LLMConfigError("missing_api_key"),
        ):
            with self.assertRaises(Exception):
                call_command("tag_knowledge", "--artifact-dir", directory)

        run = OperationRun.objects.latest("id")
        self.assertEqual(run.status, OperationRun.Status.FAILED)
        self.assertEqual(run.error_code, "tag_generation")
        self.assertEqual(run.summary["tag_inventory"], 2)
        self.assertEqual(run.summary["tag_failed_items"], 2)

    def test_artifact_directory_failure_preserves_frozen_inventory_target_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory) / "not-a-directory"
            artifact_dir.write_text("occupied")
            with self.assertRaises(KnowledgeTaggingError) as raised:
                run_tagging_pipeline(
                    artifact_dir=artifact_dir,
                    dry_run=True,
                    publish=False,
                    runner=RecordingRunner(),
                )

        self.assertEqual(raised.exception.code, "unexpected_error")
        self.assertEqual(raised.exception.summary["tag_inventory"], 2)
        self.assertEqual(raised.exception.summary["tag_failed_items"], 2)

    def test_command_dry_run_is_default(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "dashboard.management.commands.tag_knowledge.knowledge_tagging.create_default_runner",
            return_value=RecordingRunner(),
        ):
            call_command("tag_knowledge", "--artifact-dir", directory)

        run = OperationRun.objects.latest("id")
        self.assertEqual(run.status, OperationRun.Status.SUCCESS)
        self.assertTrue(run.summary["tag_dry_run"])
        self.assertFalse(run.summary["tag_published"])
        self.assertEqual(
            KnowledgeTagActiveSnapshot.objects.get().snapshot_id,
            self.initial_snapshot_id,
        )

    def test_command_records_stale_inventory_as_failed_abort(self):
        runner = CallbackReviewRunner(
            lambda: self.create_item("stale-command", self.ops, "f" * 64)
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "dashboard.management.commands.tag_knowledge.knowledge_tagging.create_default_runner",
            return_value=runner,
        ):
            call_command("tag_knowledge", "--publish", "--artifact-dir", directory)

        run = OperationRun.objects.latest("id")
        self.assertEqual(run.status, OperationRun.Status.FAILED)
        self.assertEqual(run.error_code, "tag_stale_inventory")
        self.assertTrue(run.summary["tag_stale_inventory"])
        self.assertFalse(run.summary["tag_published"])
        self.assertEqual(
            KnowledgeTagActiveSnapshot.objects.get().snapshot_id,
            self.initial_snapshot_id,
        )

    def test_command_failure_preserves_safe_aggregate_summary(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "dashboard.management.commands.tag_knowledge.knowledge_tagging.create_default_runner",
            return_value=FailingCandidateRunner(),
        ):
            with self.assertRaises(Exception):
                call_command("tag_knowledge", "--artifact-dir", directory)

        run = OperationRun.objects.latest("id")
        self.assertEqual(run.status, OperationRun.Status.FAILED)
        self.assertEqual(run.error_code, "tag_generation")
        self.assertEqual(run.summary["tag_inventory"], 2)
        self.assertEqual(run.summary["tag_candidates"], 0)
        self.assertEqual(run.summary["tag_failed_items"], 2)
        self.assertTrue(run.summary["tag_dry_run"])
        self.assertFalse(run.summary["tag_published"])
        self.assertFalse(run.summary["tag_stale_inventory"])

    def test_publish_rechecks_corpus_revision_under_fence_for_insert_phantom(self):
        runner = RecordingRunner()
        fresh = collect_knowledge_tag_inventory()
        reviewed = runner.review_tags(
            fresh.eligible,
            tuple(runner.candidate_tags(fresh.eligible)),
            runner.assign_tags(fresh.eligible, ("보안", "Security", "AWS 보안", "운영")),
        )
        self.create_item("phantom", self.ops, "f" * 64)

        with tempfile.TemporaryDirectory() as directory:
            snapshot = publish_tag_snapshot(
                inventory_digest=fresh.inventory_digest,
                corpus_revision=fresh.corpus_revision,
                manifest={"version": "test", "directory": directory},
                reviewed=reviewed,
            )

        self.assertIsNone(snapshot)
        self.assertEqual(
            KnowledgeTagActiveSnapshot.objects.get().snapshot_id,
            self.initial_snapshot_id,
        )

    def test_publish_requires_expected_corpus_revision_without_fallback_override(self):
        runner = RecordingRunner()
        fresh = collect_knowledge_tag_inventory()
        reviewed = runner.review_tags(
            fresh.eligible,
            tuple(runner.candidate_tags(fresh.eligible)),
            runner.assign_tags(fresh.eligible, ("보안", "Security", "AWS 보안", "운영")),
        )

        snapshot = publish_tag_snapshot(
            inventory_digest="0" * 64,
            corpus_revision=None,
            manifest={"version": "test"},
            reviewed=reviewed,
        )

        self.assertIsNone(snapshot)
        self.assertEqual(
            KnowledgeTagActiveSnapshot.objects.get().snapshot_id,
            self.initial_snapshot_id,
        )

    def test_publish_locks_target_items_before_corpus_revision_fence(self):
        runner = RecordingRunner()
        fresh = collect_knowledge_tag_inventory()
        reviewed = runner.review_tags(
            fresh.eligible,
            tuple(runner.candidate_tags(fresh.eligible)),
            runner.assign_tags(fresh.eligible, ("보안", "Security", "AWS 보안", "운영")),
        )

        with CaptureQueriesContext(connection) as captured:
            publish_tag_snapshot(
                inventory_digest=fresh.inventory_digest,
                corpus_revision=fresh.corpus_revision,
                manifest={"version": "test"},
                reviewed=reviewed,
            )

        statements = [query["sql"].lower() for query in captured.captured_queries]
        item_lock_index = next(
            index
            for index, sql in enumerate(statements)
            if "from \"dashboard_knowledgeitem\"" in sql
            and "dashboard_knowledgeitem\".\"id\" in" in sql
        )
        corpus_fence_index = next(
            index
            for index, sql in enumerate(statements)
            if "update \"dashboard_knowledgetagcorpusrevision\"" in sql
        )
        assignment_insert_index = next(
            index
            for index, sql in enumerate(statements)
            if "insert into \"dashboard_knowledgetagassignment\"" in sql
        )
        self.assertLess(item_lock_index, corpus_fence_index)
        self.assertLess(corpus_fence_index, assignment_insert_index)

    def test_publish_rechecks_linked_content_and_category_drift_under_fence(self):
        runner = RecordingRunner()
        fresh = collect_knowledge_tag_inventory()
        reviewed = runner.review_tags(
            fresh.eligible,
            tuple(runner.candidate_tags(fresh.eligible)),
            runner.assign_tags(fresh.eligible, ("보안", "Security", "AWS 보안", "운영")),
        )
        self.first.content_run.body = "changed body"
        self.first.content_run.save(update_fields=["body", "updated_at"])

        self.assertIsNone(
            publish_tag_snapshot(
                inventory_digest=fresh.inventory_digest,
                corpus_revision=fresh.corpus_revision,
                manifest={"version": "test"},
                reviewed=reviewed,
            )
        )

        fresh = collect_knowledge_tag_inventory()
        reviewed = runner.review_tags(
            fresh.eligible,
            tuple(runner.candidate_tags(fresh.eligible)),
            runner.assign_tags(fresh.eligible, ("보안", "Security", "AWS 보안", "운영")),
        )
        self.ops.path = "업무/운영/사고"
        self.ops.path_key = Category.canonical_path_key(self.ops.path)
        self.ops.save(update_fields=["path", "path_key"])

        self.assertIsNone(
            publish_tag_snapshot(
                inventory_digest=fresh.inventory_digest,
                corpus_revision=fresh.corpus_revision,
                manifest={"version": "test"},
                reviewed=reviewed,
            )
        )

    def test_inactive_snapshot_cleanup_keeps_bounded_history(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "dashboard.management.commands.tag_knowledge.knowledge_tagging.create_default_runner",
            side_effect=lambda: RecordingRunner(),
        ):
            for index in range(4):
                call_command("tag_knowledge", "--publish", "--artifact-dir", f"{directory}/{index}")

        inactive_count = KnowledgeTagSnapshot.objects.filter(
            status=KnowledgeTagSnapshot.Status.INACTIVE
        ).count()
        self.assertLessEqual(inactive_count, 2)

    def test_default_command_builds_and_runs_llm_runner_without_raw_artifacts(self):
        prompts = []

        def invoker(config, prompt, timeout):
            prompts.append(prompt)
            return fake_llm_invoker(config, prompt, timeout)

        with tempfile.TemporaryDirectory() as directory, patch(
            "dashboard.llm.resolve_llm_config",
            return_value=FAKE_LLM_CONFIG,
        ), patch(
            "dashboard.llm.preflight_llm",
        ), patch(
            "dashboard.knowledge_tagging.invoke_llm_tagging",
            side_effect=invoker,
        ):
            call_command("tag_knowledge", "--artifact-dir", directory)
            artifact_texts = [
                (Path(directory) / f"{artifact_name}.json").read_text()
                for artifact_name in ("inventory", "candidates", "assignments", "reviewed", "validation")
            ]

        self.assertEqual(
            [prompt["pass"] for prompt in prompts],
            ["candidate_vocabulary", "assignment", "review"],
        )
        self.assertIn("first body", json.dumps(prompts, ensure_ascii=False))
        for artifact_text in artifact_texts:
            self.assertNotIn("first body", artifact_text)

    def test_llm_runner_uses_frozen_prompt_sources_for_all_passes(self):
        prompts = []

        def invoker(config, prompt, timeout):
            prompts.append(prompt)
            if prompt["pass"] == "candidate_vocabulary":
                self.first.content_run.body = "mutated body after candidate"
                self.first.content_run.save(update_fields=["body", "updated_at"])
            return fake_llm_invoker(config, prompt, timeout)

        with tempfile.TemporaryDirectory() as directory:
            runner = LLMTaggingRunner(
                config=FAKE_LLM_CONFIG,
                invoker=invoker,
            )
            run_tagging_pipeline(
                artifact_dir=Path(directory),
                dry_run=True,
                publish=False,
                runner=runner,
            )

        first_item_texts = [
            prompt["items"][0]["text"]
            for prompt in prompts
            if prompt["items"][0]["knowledge_item_id"] == self.first.pk
        ]
        self.assertEqual(first_item_texts, ["first body", "first body", "first body"])

    def test_command_produces_only_expected_artifact_files(self):
        runner_calls = []

        def runner_factory():
            runner_calls.append(True)
            return RecordingRunner()

        with tempfile.TemporaryDirectory() as directory, patch(
            "dashboard.management.commands.tag_knowledge.knowledge_tagging.create_default_runner",
            side_effect=runner_factory,
        ):
            call_command("tag_knowledge", "--artifact-dir", directory)
            artifact_names = sorted(path.name for path in Path(directory).iterdir())

        self.assertEqual(
            artifact_names,
            ["assignments.json", "candidates.json", "inventory.json", "reviewed.json", "validation.json"],
        )
        self.assertEqual(len(runner_calls), 1)

    def test_pipeline_creates_explicit_artifact_dir_before_default_runner(self):
        with tempfile.TemporaryDirectory() as parent:
            artifact_dir = Path(parent) / "nested" / "artifacts"
            seen = {}

            def runner_factory():
                seen["exists"] = artifact_dir.exists()
                return RecordingRunner()

            with patch(
                "dashboard.knowledge_tagging.create_default_runner",
                side_effect=runner_factory,
            ):
                run_tagging_pipeline(
                    artifact_dir=artifact_dir,
                    dry_run=True,
                    publish=False,
                )

            self.assertTrue(seen["exists"])
            self.assertTrue((artifact_dir / "inventory.json").exists())

    def test_llm_runner_batches_candidate_assignment_and_review_in_order(self):
        self.create_item("third", self.ops, "d" * 64)
        inventory = collect_knowledge_tag_inventory().eligible
        prompts = []
        with tempfile.TemporaryDirectory() as directory:
            runner = LLMTaggingRunner(
                config=FAKE_LLM_CONFIG,
                batch_size=1,
                invoker=lambda config, prompt, timeout: (
                    prompts.append(prompt) or fake_llm_invoker(config, prompt, timeout)
                ),
            )
            result = run_tagging_pipeline(
                artifact_dir=Path(directory),
                dry_run=True,
                publish=False,
                runner=runner,
            )

        self.assertEqual(result.summary["tag_inventory"], 3)
        self.assertEqual(
            [prompt["pass"] for prompt in prompts],
            [
                "candidate_vocabulary",
                "candidate_vocabulary",
                "candidate_vocabulary",
                "assignment",
                "assignment",
                "assignment",
                "review",
                "review",
                "review",
            ],
        )
        self.assertNotIn("current_candidates", prompts[0])
        self.assertNotIn("current_candidates", prompts[1])
        self.assertNotIn("current_candidates", prompts[2])

    def test_candidate_batches_do_not_replay_prior_candidates_and_union_in_scan_order(self):
        self.create_item("third", self.ops, "d" * 64)
        inventory = collect_knowledge_tag_inventory().eligible
        prompts = []
        large_prior_output = [f"prior-{index}" for index in range(120)]
        responses = [
            ["Alpha", "Beta", *large_prior_output],
            ["alpha", "Gamma"],
            ["beta", "Delta"],
        ]

        def invoker(config, prompt, timeout):
            prompts.append(prompt)
            if prompt["pass"] == "candidate_vocabulary":
                return json.dumps({"tags": responses.pop(0)}, ensure_ascii=False)
            return fake_llm_invoker(config, prompt, timeout)

        with tempfile.TemporaryDirectory() as directory:
            runner = LLMTaggingRunner(
                config=FAKE_LLM_CONFIG,
                batch_size=1,
                invoker=invoker,
            )
            candidates = runner.candidate_tags(inventory)

        candidate_prompts = [
            prompt for prompt in prompts if prompt["pass"] == "candidate_vocabulary"
        ]
        self.assertEqual(len(candidate_prompts), 3)
        for prompt in candidate_prompts:
            serialized = json.dumps(prompt, ensure_ascii=False)
            self.assertNotIn("current_candidates", prompt)
            self.assertNotIn("prior-119", serialized)
        self.assertEqual(
            candidates,
            ["Alpha", "Beta", *large_prior_output, "Gamma", "Delta"],
        )

    def test_strict_parser_rejects_duplicate_keys_nan_and_unknown_ids(self):
        inventory = collect_knowledge_tag_inventory().eligible
        with tempfile.TemporaryDirectory() as directory:
            runner = LLMTaggingRunner(
                config=FAKE_LLM_CONFIG,
                invoker=lambda *_args: '{"tags":["보안"],"tags":["운영"]}',
            )
            with self.assertRaises(KnowledgeTaggingError):
                runner.candidate_tags(inventory)

            runner = LLMTaggingRunner(
                config=FAKE_LLM_CONFIG,
                invoker=lambda *_args: '{"tags":[NaN]}',
            )
            with self.assertRaises(KnowledgeTaggingError):
                runner.candidate_tags(inventory)

            runner = LLMTaggingRunner(
                config=FAKE_LLM_CONFIG,
                invoker=lambda *_args: '{"items":[{"knowledge_item_id":999,"tags":["보안","Security","AWS 보안"]}]}',
            )
            with self.assertRaises(KnowledgeTaggingError):
                runner.assign_tags(inventory, ("보안", "Security", "AWS 보안"))

    def test_cross_artifact_substitution_is_rejected_after_manifest_recalculation(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_tagging_pipeline(
                artifact_dir=Path(directory),
                dry_run=True,
                publish=False,
                runner=RecordingRunner(),
            )
            assignments_path = Path(directory) / "assignments.json"
            assignments = json.loads(assignments_path.read_text())
            assignments["items"] = assignments["items"][:1]
            assignments_path.write_text(
                json.dumps(assignments, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            )
            manifest = {
                **result.artifacts.manifest,
                "assignments": {
                    **result.artifacts.manifest["assignments"],
                    "sha256": hashlib.sha256(assignments_path.read_text().encode()).hexdigest(),
                    "bytes": len(assignments_path.read_bytes()),
                },
            }

            with self.assertRaises(KnowledgeTaggingError):
                validate_tagging_artifacts(Path(directory), manifest)

    def test_artifact_validation_rejects_assignment_tags_outside_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_tagging_pipeline(
                artifact_dir=Path(directory),
                dry_run=True,
                publish=False,
                runner=RecordingRunner(),
            )
            assignments_path = Path(directory) / "assignments.json"
            assignments = json.loads(assignments_path.read_text())
            assignments["items"][0]["tags"] = ["보안", "Security", "후보밖"]
            assignments_path.write_text(
                json.dumps(assignments, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            )
            manifest = {
                **result.artifacts.manifest,
                "assignments": {
                    **result.artifacts.manifest["assignments"],
                    "sha256": hashlib.sha256(assignments_path.read_text().encode()).hexdigest(),
                    "bytes": len(assignments_path.read_bytes()),
                },
            }

            with self.assertRaises(KnowledgeTaggingError):
                validate_tagging_artifacts(Path(directory), manifest)
