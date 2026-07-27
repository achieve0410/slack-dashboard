import logging
import os
import re
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard import llm
from dashboard.operation_runs import finish_operation, prune_operation_runs, start_operation
from dashboard.quiz_generation import (
    MAX_ATTEMPTS,
    MAX_BATCH_SIZE,
    MODEL,
    PROVIDER,
    quiz_generation_lock,
    run_generation_batch,
)


logger = logging.getLogger(__name__)


def safe_log_token(value) -> str:
    return re.sub(r"[^A-Za-z0-9._:/-]", "_", str(value))[:100]


class Command(BaseCommand):
    help = "LLM으로 지식 기반 퀴즈 문제를 생성합니다."

    def add_arguments(self, parser):
        parser.add_argument("--publish", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--inventory-only", action="store_true")
        parser.add_argument("--domain", choices=["english", "japanese", "aws_saa"])
        parser.add_argument("--item-id", type=int)
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
        parser.add_argument(
            "--aws-allowlist-external-id",
            action="append",
            default=[],
        )
        parser.add_argument(
            "--aws-allowlist-source-key",
            action="append",
            default=[],
        )

    def handle(self, *args, **options):
        attempt = start_operation("quiz", logger=logger)
        try:
            summary = self._generate_command(*args, **options)
        except CommandError as error:
            error_code = (
                "quiz_generation"
                if str(error).startswith("LLM 설정 오류")
                else "configuration_error"
            )
            finish_operation(
                attempt,
                "failed",
                error_code=error_code,
                logger=logger,
            )
            raise
        except Exception:
            finish_operation(
                attempt,
                "failed",
                error_code="unexpected_error",
                logger=logger,
            )
            raise
        else:
            finish_operation(
                attempt,
                "skipped" if summary is None else "success",
                error_code="lock_contended" if summary is None else "",
                summary=summary or {},
                logger=logger,
            )
        finally:
            prune_operation_runs(logger=logger)

    def _generate_command(self, *args, **options):
        item_id = options["item_id"]
        limit = options["limit"]
        max_attempts = options["max_attempts"]
        if options["dry_run"] and options["publish"]:
            raise CommandError("--dry-run과 --publish는 함께 사용할 수 없습니다.")
        if item_id is not None and item_id <= 0:
            raise CommandError("--item-id는 양의 정수여야 합니다.")
        if limit < 1 or limit > MAX_BATCH_SIZE:
            raise CommandError(f"--limit는 1~{MAX_BATCH_SIZE}이어야 합니다.")
        if max_attempts < 1 or max_attempts > MAX_ATTEMPTS:
            raise CommandError(f"--max-attempts는 1~{MAX_ATTEMPTS}이어야 합니다.")
        try:
            timeout = int(os.getenv("LLM_TIMEOUT", "180"))
        except ValueError as error:
            raise CommandError("LLM_TIMEOUT은 정수여야 합니다.") from error
        if timeout < 1:
            raise CommandError("LLM_TIMEOUT은 1 이상이어야 합니다.")

        lock_path = Path(settings.BASE_DIR) / "run" / "generate_quiz_questions.lock"
        with quiz_generation_lock(lock_path) as acquired:
            if not acquired:
                self.stdout.write("퀴즈 생성 작업이 이미 실행 중이므로 건너뜁니다.")
                return None
            config = None
            if not options["inventory_only"]:
                try:
                    config = llm.resolve_llm_config()
                    llm.preflight_llm(config)
                except llm.LLMConfigError as error:
                    raise CommandError(f"LLM 설정 오류: {error}") from error
            return self._generate(
                config=config,
                timeout=timeout,
                dry_run=not options["publish"],
                inventory_only=options["inventory_only"],
                domain=options["domain"],
                item_id=item_id,
                limit=limit,
                max_attempts=max_attempts,
                aws_allowlisted_external_ids=options[
                    "aws_allowlist_external_id"
                ],
                aws_allowlisted_source_keys=options[
                    "aws_allowlist_source_key"
                ],
            )

    def _generate(self, **kwargs) -> dict:
        started_at = time.monotonic()
        summary = run_generation_batch(**kwargs)
        elapsed_ms = round((time.monotonic() - started_at) * 1000)
        summary = {**summary, "elapsed_ms": elapsed_ms}
        self.stdout.write(
            self.style.SUCCESS(
                "quiz_generation_summary "
                + " ".join(f"{key}={value}" for key, value in summary.items())
                + f" model={safe_log_token(MODEL)}"
                + f" provider={safe_log_token(PROVIDER)}"
            )
        )
        return summary
