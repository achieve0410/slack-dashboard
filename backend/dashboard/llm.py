"""Thin adapter over the Anthropic and OpenAI SDKs.

This is the single seam the classification, tagging, and quiz-generation
pipelines call through to reach an LLM. It intentionally does not use
provider-side structured outputs: callers pass a prompt that asks for a
single JSON object and parse the returned text themselves with a strict
parser, so the response contract is enforced identically regardless of
provider.
"""

import logging
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
JSON_SYSTEM_PROMPT = "Respond with exactly one JSON object and nothing else."
SUPPORTED_OPERATIONS = frozenset({"classify", "tagging", "quiz", "ask"})


logger = logging.getLogger(__name__)


class LLMConfigError(Exception):
    """Raised for problems that require the operator to fix configuration."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class LLMTransportError(Exception):
    """Raised for problems with a single call that may succeed on retry."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: str
    max_tokens: int


@dataclass(frozen=True)
class LLMResponse:
    text: str
    usage: dict


def configured_provider_name() -> str:
    return os.getenv("LLM_PROVIDER", "anthropic").strip().lower()


def configured_model_name() -> str:
    """Best-effort model label for module-level constants and logging.

    Never raises: callers that need a validated, ready-to-use configuration
    should call resolve_llm_config() instead.
    """
    model = os.getenv("LLM_MODEL", "").strip()
    if model:
        return model
    return DEFAULT_ANTHROPIC_MODEL if configured_provider_name() != "openai" else ""


def resolve_llm_config() -> LLMConfig:
    provider = configured_provider_name()
    if provider not in {"anthropic", "openai"}:
        raise LLMConfigError("unsupported_provider")

    model = os.getenv("LLM_MODEL", "").strip()
    if not model and provider == "anthropic":
        model = DEFAULT_ANTHROPIC_MODEL
    if not model:
        raise LLMConfigError("missing_model")

    api_key = os.getenv(
        "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY",
        "",
    ).strip()
    if not api_key:
        raise LLMConfigError("missing_api_key")

    try:
        max_tokens = int(os.getenv("LLM_MAX_TOKENS", "8192"))
    except ValueError as error:
        raise LLMConfigError("invalid_max_tokens") from error
    if max_tokens < 1:
        raise LLMConfigError("invalid_max_tokens")

    return LLMConfig(provider=provider, model=model, api_key=api_key, max_tokens=max_tokens)


def preflight_llm(config: LLMConfig) -> None:
    """Validate the SDK is importable. Makes no network calls."""
    if config.provider == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError as error:
            raise LLMConfigError("sdk_not_installed") from error
    else:
        try:
            import openai  # noqa: F401
        except ImportError as error:
            raise LLMConfigError("sdk_not_installed") from error


def complete(
    config: LLMConfig,
    prompt: str,
    *,
    timeout: int,
    operation: str,
) -> LLMResponse:
    if operation not in SUPPORTED_OPERATIONS:
        raise LLMConfigError("invalid_operation")
    cost_rates = _cost_rates()
    _enforce_daily_budget()
    if config.provider == "anthropic":
        response = _anthropic_complete(config, prompt, timeout=timeout)
    else:
        response = _openai_complete(config, prompt, timeout=timeout)
    _record_usage(operation, config, response.usage, cost_rates=cost_rates)
    return response


def usage_summary(*, now=None, tolerate_invalid: bool = False) -> dict:
    from django.apps import apps
    from django.db.models import Sum
    from django.utils import timezone

    checked_at = now or timezone.now()
    day_start = checked_at.astimezone(timezone.get_current_timezone()).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    model = apps.get_model("dashboard", "LLMUsageRecord")
    totals = model.objects.filter(created_at__gte=day_start).aggregate(
        api_calls=Sum("api_calls"),
        total_tokens=Sum("total_tokens"),
        estimated_cost_usd=Sum("estimated_cost_usd"),
    )
    configuration_error = ""
    try:
        limits = _budget_limits()
        _cost_rates()
    except LLMConfigError as error:
        if not tolerate_invalid:
            raise
        configuration_error = error.code
        limits = {
            "api_calls": 0,
            "total_tokens": 0,
            "estimated_cost_usd": Decimal("0"),
        }
    used = {
        "api_calls": totals["api_calls"] or 0,
        "total_tokens": totals["total_tokens"] or 0,
        "estimated_cost_usd": totals["estimated_cost_usd"] or Decimal("0"),
    }
    return {
        "day_start": day_start,
        "used": used,
        "limits": limits,
        "blocked": bool(configuration_error) or _budget_exceeded(used, limits),
        "configuration_error": configuration_error,
    }


def _budget_limits() -> dict:
    return {
        "api_calls": _non_negative_int("LLM_DAILY_API_CALL_LIMIT"),
        "total_tokens": _non_negative_int("LLM_DAILY_TOKEN_LIMIT"),
        "estimated_cost_usd": _non_negative_decimal("LLM_DAILY_COST_USD_LIMIT"),
    }


def _non_negative_int(name: str) -> int:
    raw = os.getenv(name, "0").strip() or "0"
    try:
        value = int(raw)
    except ValueError as error:
        raise LLMConfigError("invalid_budget") from error
    if value < 0:
        raise LLMConfigError("invalid_budget")
    return value


def _non_negative_decimal(name: str) -> Decimal:
    raw = os.getenv(name, "0").strip() or "0"
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise LLMConfigError("invalid_budget") from error
    if not value.is_finite() or value < 0:
        raise LLMConfigError("invalid_budget")
    return value


def _cost_rates() -> tuple[Decimal, Decimal]:
    return (
        _non_negative_decimal("LLM_INPUT_COST_PER_MTOK_USD"),
        _non_negative_decimal("LLM_OUTPUT_COST_PER_MTOK_USD"),
    )


def _budget_exceeded(used: dict, limits: dict) -> bool:
    return any(
        limits[key] > 0 and used[key] >= limits[key]
        for key in ("api_calls", "total_tokens", "estimated_cost_usd")
    )


def _enforce_daily_budget() -> None:
    summary = usage_summary()
    if summary["blocked"]:
        raise LLMConfigError("daily_budget_exceeded")


def _record_usage(
    operation: str,
    config: LLMConfig,
    usage: dict,
    *,
    cost_rates: tuple[Decimal, Decimal],
) -> None:
    from django.apps import apps

    input_tokens = max(0, int(usage.get("input_tokens") or 0))
    output_tokens = max(0, int(usage.get("output_tokens") or 0))
    total_tokens = max(
        input_tokens + output_tokens,
        int(usage.get("total_tokens") or 0),
    )
    input_rate, output_rate = cost_rates
    estimated_cost = (
        Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate
    ) / Decimal(1_000_000)
    model = apps.get_model("dashboard", "LLMUsageRecord")
    try:
        model.objects.create(
            operation=operation,
            provider=config.provider,
            model_name=str(usage.get("model") or config.model),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            api_calls=max(1, int(usage.get("api_calls") or 1)),
            estimated_cost_usd=estimated_cost,
        )
    except Exception:
        logger.exception("llm_usage_record_failed operation=%s", operation)


def _anthropic_complete(config: LLMConfig, prompt: str, *, timeout: int) -> LLMResponse:
    import anthropic

    client = anthropic.Anthropic(api_key=config.api_key, timeout=float(timeout), max_retries=2)
    try:
        response = client.messages.create(
            model=config.model,
            max_tokens=config.max_tokens,
            system=JSON_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError as error:
        raise LLMConfigError("invalid_api_key") from error
    except (
        anthropic.APITimeoutError,
        anthropic.APIConnectionError,
        anthropic.RateLimitError,
        anthropic.APIStatusError,
    ) as error:
        raise LLMTransportError("api_error") from error

    if response.stop_reason == "refusal":
        raise LLMTransportError("refusal")
    if response.stop_reason == "max_tokens":
        raise LLMTransportError("truncated")

    text = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )
    usage = {
        "model": response.model,
        "provider": "anthropic",
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
        "api_calls": 1,
    }
    return LLMResponse(text=text, usage=usage)


def _openai_complete(config: LLMConfig, prompt: str, *, timeout: int) -> LLMResponse:
    import openai
    from openai import OpenAI

    client = OpenAI(api_key=config.api_key, timeout=float(timeout), max_retries=2)
    try:
        response = client.chat.completions.create(
            model=config.model,
            max_completion_tokens=config.max_tokens,
            messages=[
                {"role": "system", "content": JSON_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
    except openai.AuthenticationError as error:
        raise LLMConfigError("invalid_api_key") from error
    except (
        openai.APITimeoutError,
        openai.APIConnectionError,
        openai.RateLimitError,
        openai.APIStatusError,
    ) as error:
        raise LLMTransportError("api_error") from error

    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise LLMTransportError("truncated")
    if choice.finish_reason == "content_filter":
        raise LLMTransportError("refusal")

    usage = {
        "model": response.model,
        "provider": "openai",
        "input_tokens": response.usage.prompt_tokens,
        "output_tokens": response.usage.completion_tokens,
        "total_tokens": response.usage.total_tokens,
        "api_calls": 1,
    }
    return LLMResponse(text=choice.message.content or "", usage=usage)
