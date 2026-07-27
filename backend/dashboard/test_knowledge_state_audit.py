import json
import stat
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import TestCase

from .models import (
    ContentRun,
    CronJob,
    KnowledgeConsumptionState,
    KnowledgeItem,
    UserRunState,
)


class KnowledgeStateAuditCommandTests(TestCase):
    def setUp(self):
        job = CronJob.objects.create(external_id="audit", name="감사")
        self.visible_run = ContentRun.objects.create(
            job=job,
            external_ts="100.100",
            status=ContentRun.Status.SUCCESS,
            title="보이는 실행",
            body="본문",
            generated_at=datetime(2026, 7, 16, tzinfo=UTC),
        )
        self.hidden_run = ContentRun.objects.create(
            job=job,
            external_ts="100.200",
            status=ContentRun.Status.SUCCESS,
            title="숨김 실행",
            body="본문",
            generated_at=datetime(2026, 7, 16, 0, 1, tzinfo=UTC),
            hidden_at=datetime(2026, 7, 16, 1, tzinfo=UTC),
        )
        self.orphan_run = ContentRun.objects.create(
            job=job,
            external_ts="100.300",
            status=ContentRun.Status.SUCCESS,
            title="지식 없는 실행",
            body="본문",
            generated_at=datetime(2026, 7, 16, 0, 2, tzinfo=UTC),
        )
        self.visible_item = self.create_item(self.visible_run, "cron:audit:visible")
        self.hidden_item = self.create_item(self.hidden_run, "cron:audit:hidden")

        UserRunState.objects.create(
            run=self.visible_run,
            session_key="session-secret-a",
            bookmarked=True,
            note="secret note alpha",
        )
        UserRunState.objects.create(
            run=self.visible_run,
            session_key="session-secret-b",
            completed=True,
            note="secret note beta",
        )
        UserRunState.objects.create(
            run=self.visible_run,
            session_key="session-secret-c",
            note="",
        )
        UserRunState.objects.create(
            run=self.hidden_run,
            session_key="session-hidden",
            bookmarked=True,
            completed=True,
        )
        KnowledgeConsumptionState.objects.create(
            knowledge_item=self.visible_item,
            bookmarked_at=datetime(2026, 7, 16, 2, tzinfo=UTC),
            note="canonical secret note",
        )

    @staticmethod
    def create_item(run: ContentRun, source_key: str) -> KnowledgeItem:
        return KnowledgeItem.objects.create(
            source_type=KnowledgeItem.SourceType.CRON,
            source_key=source_key,
            content_run=run,
            status=KnowledgeItem.Status.PENDING,
            title=run.title,
            source_hash="a" * 64,
            generated_at=run.generated_at,
        )

    def run_command(self, *args: str) -> tuple[dict, str]:
        stdout = StringIO()
        call_command("audit_knowledge_state", *args, stdout=stdout)
        raw = stdout.getvalue()
        return json.loads(raw), raw

    def test_reports_counts_conflicts_and_ids_without_raw_notes_or_sessions(self):
        payload, raw = self.run_command()

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(
            payload["counts"]["content_runs"],
            {"total": 3, "visible": 2, "hidden": 1},
        )
        self.assertEqual(
            payload["counts"]["knowledge_items"],
            {"total": 2, "visible": 2, "hidden": 0},
        )
        self.assertEqual(
            payload["counts"]["user_run_states"],
            {"total": 4, "for_visible_runs": 3, "for_hidden_runs": 1},
        )
        self.assertEqual(
            payload["counts"]["knowledge_consumption_states"],
            {"total": 1, "for_visible_items": 1, "for_hidden_items": 0},
        )
        self.assertEqual(payload["legacy_orphans"]["count"], 1)
        self.assertEqual(payload["legacy_orphans"]["content_run_ids"], [self.orphan_run.id])
        self.assertEqual(payload["hidden_pair_mismatches"]["count"], 1)
        self.assertEqual(
            payload["hidden_pair_mismatches"]["content_run_ids"],
            [self.hidden_run.id],
        )
        self.assertEqual(payload["multiple_session_runs"]["count"], 1)
        self.assertEqual(
            payload["multiple_session_runs"]["content_run_ids"],
            [self.visible_run.id],
        )
        self.assertEqual(payload["note_conflicts"]["empty_note_state_count"], 2)
        self.assertEqual(payload["note_conflicts"]["non_empty_note_state_count"], 2)
        self.assertEqual(
            payload["note_conflicts"]["empty_and_non_empty_content_run_ids"],
            [self.visible_run.id],
        )
        self.assertEqual(
            payload["note_conflicts"]["distinct_non_empty_content_run_ids"],
            [self.visible_run.id],
        )
        self.assertEqual(payload["state_flags"]["bookmarked"]["true_count"], 2)
        self.assertEqual(payload["state_flags"]["completed"]["true_count"], 2)
        self.assertEqual(
            payload["canonical_state_flags"]["bookmarked"]["true_count"], 1
        )
        self.assertEqual(payload["canonical_state_flags"]["read"]["true_count"], 0)
        self.assertNotIn("secret note", raw)
        self.assertNotIn("session-secret", raw)

    def test_output_file_is_atomic_private_and_matches_stdout(self):
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        output = Path(temporary_directory.name) / "audit.json"

        payload, raw = self.run_command("--output", str(output))

        self.assertEqual(json.loads(output.read_text()), payload)
        self.assertEqual(json.loads(raw), payload)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_command_is_read_only(self):
        before = {
            "runs": list(ContentRun.objects.values_list("id", "updated_at")),
            "items": list(KnowledgeItem.objects.values_list("id", "updated_at")),
            "states": list(UserRunState.objects.values_list("id", "updated_at")),
            "consumption_states": list(
                KnowledgeConsumptionState.objects.values_list("id", "updated_at")
            ),
        }

        self.run_command()

        self.assertEqual(
            before,
            {
                "runs": list(ContentRun.objects.values_list("id", "updated_at")),
                "items": list(KnowledgeItem.objects.values_list("id", "updated_at")),
                "states": list(UserRunState.objects.values_list("id", "updated_at")),
                "consumption_states": list(
                    KnowledgeConsumptionState.objects.values_list("id", "updated_at")
                ),
            },
        )
