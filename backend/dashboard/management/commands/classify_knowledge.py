import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard import llm
from dashboard.classification import (
    MAX_BATCH_SIZE,
    MODEL,
    PROVIDER,
    ClassifierValidationError,
    TransientInferenceError,
    active_category_catalog,
    apply_decision,
    classifier_lock,
    eligible_pending_items,
    invoke_llm,
    mark_invalid_output,
)
from dashboard.operation_runs import (
    finish_operation,
    prune_operation_runs,
    start_operation,
)


logger = logging.getLogger(__name__)


def safe_log_token(value) -> str:
    return re.sub(r"[^A-Za-z0-9._:/-]", "_", str(value))[:100]


def safe_log_path(value) -> str:
    return re.sub(r"[\x00-\x1f\x7f\s]+", "_", str(value))[:400]


def usage_total(reports: list[dict], key: str) -> Decimal:
    total = Decimal("0")
    for report in reports:
        value = report.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            total += Decimal(str(value))
        except InvalidOperation:
            continue
    return total


class Command(BaseCommand):
    help = "분류 대기 중인 지식 항목을 LLM으로 분류합니다."

    def add_arguments(self, parser):
        parser.add_argument("--item-id", type=int)
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        attempt = start_operation("classify", logger=logger)
        try:
            summary = self._classify_command(*args, **options)
        except CommandError as error:
            error_code = (
                "classifier_initialization"
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

    def _classify_command(self, *args, **options):
        item_id = options["item_id"]
        limit = options["limit"]
        if item_id is not None and item_id <= 0:
            raise CommandError("--item-id는 양의 정수여야 합니다.")
        if limit < 1 or limit > MAX_BATCH_SIZE:
            raise CommandError(f"--limit는 1~{MAX_BATCH_SIZE}이어야 합니다.")
        try:
            timeout = int(os.getenv("LLM_TIMEOUT", "180"))
        except ValueError as error:
            raise CommandError("LLM_TIMEOUT은 정수여야 합니다.") from error
        if timeout < 1:
            raise CommandError("LLM_TIMEOUT은 1 이상이어야 합니다.")

        lock_path = Path(settings.BASE_DIR) / "run" / "classify_knowledge.lock"
        with classifier_lock(lock_path) as acquired:
            if not acquired:
                self.stdout.write("분류 작업이 이미 실행 중이므로 건너뜁니다.")
                return
            try:
                config = llm.resolve_llm_config()
                llm.preflight_llm(config)
            except llm.LLMConfigError as error:
                raise CommandError(f"LLM 설정 오류: {error}") from error
            return self._classify(
                config=config,
                timeout=timeout,
                item_id=item_id,
                limit=limit,
            )

    def _classify(
        self,
        *,
        config: llm.LLMConfig,
        timeout: int,
        item_id: int | None,
        limit: int,
    ) -> None:
        started_at = time.monotonic()
        items = eligible_pending_items(item_id, limit)
        counts = {
            "selected": len(items),
            "classified": 0,
            "needs_review": 0,
            "stale": 0,
            "transient_failure": 0,
            "missing_usage": 0,
            "category_created": 0,
            "category_reused": 0,
            "category_existing": 0,
        }
        usage_reports = []
        self.stdout.write(
            f"classification_start selected={len(items)} model={MODEL} provider={PROVIDER}"
        )
        for item in items:
            expected_hash = item.source_hash
            catalog = active_category_catalog()
            try:
                result = invoke_llm(
                    config,
                    item,
                    catalog,
                    timeout,
                )
            except TransientInferenceError as error:
                counts["transient_failure"] += 1
                self.stderr.write(
                    f"item={item.pk} error={safe_log_token(error.code)} "
                    f"exception={type(error).__name__}"
                )
                continue
            except ClassifierValidationError as error:
                try:
                    outcome = mark_invalid_output(item.pk, expected_hash, error.code)
                except Exception as apply_error:
                    counts["transient_failure"] += 1
                    logger.exception(
                        "classifier_unexpected item=%s stage=invalid_output_apply exception=%s",
                        item.pk,
                        type(apply_error).__name__,
                    )
                    self.stderr.write(
                        f"item={item.pk} error=invalid_output_apply_failure "
                        f"exception={type(apply_error).__name__}"
                    )
                    continue
                counts[outcome] += 1
                self.stderr.write(
                    f"item={item.pk} error={safe_log_token(error.code)} "
                    f"exception={type(error).__name__} outcome={outcome}"
                )
                continue

            observation = {}
            try:
                outcome = apply_decision(
                    item.pk,
                    expected_hash,
                    result.decision,
                    observation,
                )
            except Exception as error:
                counts["transient_failure"] += 1
                logger.exception(
                    "classifier_unexpected item=%s stage=classification_apply exception=%s",
                    item.pk,
                    type(error).__name__,
                )
                self.stderr.write(
                    f"item={item.pk} error=classification_apply_failure "
                    f"exception={type(error).__name__}"
                )
                continue
            counts[outcome] += 1
            for key in ("category_created", "category_reused", "category_existing"):
                counts[key] += observation[key]
            if outcome == "classified":
                for path in observation["category_growth_paths"]:
                    self.stdout.write(
                        f"category_growth item={item.pk} "
                        f"confidence={safe_log_token(result.decision.confidence)} "
                        f"path={safe_log_path(path)}"
                    )
            if result.usage:
                usage_reports.append(result.usage)
                usage_state = "present"
            else:
                counts["missing_usage"] += 1
                usage_state = "missing"
            self.stdout.write(
                f"classification_item item={item.pk} outcome={outcome} "
                f"usage={usage_state} category_created={observation['category_created']} "
                f"category_reused={observation['category_reused']} "
                f"category_existing={observation['category_existing']}"
            )

        elapsed_ms = round((time.monotonic() - started_at) * 1000)
        self.stdout.write(
            self.style.SUCCESS(
                "classification_summary "
                + " ".join(f"{key}={value}" for key, value in counts.items())
                + f" usage_reports={len(usage_reports)}"
                + f" model={MODEL}"
                + f" provider={PROVIDER}"
                + f" elapsed_ms={elapsed_ms}"
                + f" input_tokens={usage_total(usage_reports, 'input_tokens')}"
                + f" output_tokens={usage_total(usage_reports, 'output_tokens')}"
                + f" api_calls={usage_total(usage_reports, 'api_calls')}"
                + " estimated_cost_usd="
                + str(usage_total(usage_reports, "estimated_cost_usd"))
            )
        )
        return {
            **counts,
            "usage_reports": len(usage_reports),
            "elapsed_ms": elapsed_ms,
        }
