import logging
import re
import tempfile
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard import knowledge_tagging
from dashboard.knowledge_tagging import (
    KnowledgeTaggingError,
)
from dashboard.operation_runs import finish_operation, prune_operation_runs, start_operation
from dashboard.quiz_generation import quiz_generation_lock


logger = logging.getLogger(__name__)


def safe_log_token(value) -> str:
    return re.sub(r"[^A-Za-z0-9._:/-]", "_", str(value))[:120]


class Command(BaseCommand):
    help = "전체 비학습 지식에 대한 3-pass 태그 스냅샷을 생성합니다."

    def add_arguments(self, parser):
        parser.add_argument("--publish", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--artifact-dir",
            help="태깅 산출물을 저장할 디렉터리입니다. 미지정 시 임시 디렉터리를 사용합니다.",
        )

    def handle(self, *args, **options):
        attempt = start_operation("tagging", logger=logger)
        started_at = time.monotonic()
        try:
            summary = self._tag_command(operation_run_id=attempt.run_id, **options)
        except CommandError:
            finish_operation(
                attempt,
                "failed",
                error_code="configuration_error",
                logger=logger,
            )
            raise
        except KnowledgeTaggingError as error:
            status = "skipped" if error.code == "lock_contended" else "failed"
            summary = getattr(error, "summary", None) or _safe_failure_summary(options)
            if summary:
                summary = {
                    **summary,
                    "elapsed_ms": round((time.monotonic() - started_at) * 1000),
                }
            finish_operation(
                attempt,
                status,
                error_code=error.code,
                summary=summary,
                logger=logger,
            )
            if error.code == "lock_contended":
                self.stdout.write("태깅 작업이 이미 실행 중이므로 건너뜁니다.")
                return
            raise CommandError(f"지식 태깅 실패: {error.code}") from error
        except Exception:
            summary = {
                **_safe_failure_summary(options),
                "elapsed_ms": round((time.monotonic() - started_at) * 1000),
            }
            finish_operation(
                attempt,
                "failed",
                error_code="unexpected_error",
                summary=summary,
                logger=logger,
            )
            raise
        else:
            status = "failed" if summary.get("tag_stale_inventory") else "success"
            finish_operation(
                attempt,
                status,
                error_code="tag_stale_inventory" if summary.get("tag_stale_inventory") else "",
                summary=summary,
                logger=logger,
            )
        finally:
            prune_operation_runs(logger=logger)

    def _tag_command(self, *, operation_run_id: int | None, **options) -> dict:
        publish = options["publish"]
        dry_run = options["dry_run"] or not publish
        if publish and options["dry_run"]:
            raise CommandError("--dry-run과 --publish는 함께 사용할 수 없습니다.")

        lock_path = Path(
            getattr(
                settings,
                "TAG_KNOWLEDGE_LOCK_PATH",
                Path(settings.BASE_DIR) / "run" / "tag_knowledge.lock",
            )
        )
        with quiz_generation_lock(lock_path) as acquired:
            if not acquired:
                raise KnowledgeTaggingError("lock_contended")
            if options.get("artifact_dir"):
                artifact_dir = Path(options["artifact_dir"]).expanduser().resolve()
                return self._run(
                    artifact_dir=artifact_dir,
                    dry_run=dry_run,
                    publish=publish,
                    operation_run_id=operation_run_id,
                )
            with tempfile.TemporaryDirectory(
                prefix="slack-dashboard-tags-"
            ) as temporary_directory:
                return self._run(
                    artifact_dir=Path(temporary_directory),
                    dry_run=dry_run,
                    publish=publish,
                    operation_run_id=operation_run_id,
                )

    def _run(
        self,
        *,
        artifact_dir: Path,
        dry_run: bool,
        publish: bool,
        operation_run_id: int | None,
    ) -> dict:
        started_at = time.monotonic()
        try:
            inventory_result = knowledge_tagging.collect_knowledge_tag_inventory()
        except RuntimeError as error:
            raise KnowledgeTaggingError(
                "tag_inventory",
                summary=_safe_failure_summary(
                    {"dry_run": dry_run, "publish": publish},
                    inventory_count=0,
                ),
            ) from error
        try:
            runner = knowledge_tagging.create_default_runner()
        except KnowledgeTaggingError as error:
            error.summary = _safe_failure_summary(
                {"dry_run": dry_run, "publish": publish},
                inventory_count=len(inventory_result.eligible),
            )
            raise
        result = knowledge_tagging.run_tagging_pipeline(
            artifact_dir=artifact_dir,
            dry_run=dry_run,
            publish=publish,
            runner=runner,
            inventory_result=inventory_result,
            operation_run_id=operation_run_id,
        )
        elapsed_ms = round((time.monotonic() - started_at) * 1000)
        summary = {**result.summary, "elapsed_ms": elapsed_ms}
        self.stdout.write(
            self.style.SUCCESS(
                "knowledge_tagging_summary "
                + " ".join(f"{key}={value}" for key, value in summary.items())
                + f" artifacts={safe_log_token(result.artifacts.directory)}"
            )
        )
        return summary


def _safe_failure_summary(options, *, inventory_count: int = 0) -> dict:
    publish = bool(options.get("publish"))
    dry_run = bool(options.get("dry_run")) or not publish
    return {
        "tag_inventory": inventory_count,
        "tag_candidates": 0,
        "tag_assigned_items": 0,
        "tag_assignments": 0,
        "tag_reviewed_items": 0,
        "tag_failed_items": inventory_count,
        "tag_dry_run": dry_run,
        "tag_published": False,
        "tag_stale_inventory": False,
    }
