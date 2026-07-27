"""Thin adapter over the Anthropic and OpenAI SDKs.

This is the single seam the classification, tagging, and quiz-generation
pipelines call through to reach an LLM. It intentionally does not use
provider-side structured outputs: callers pass a prompt that asks for a
single JSON object and parse the returned text themselves with a strict
parser, so the response contract is enforced identically regardless of
provider.
"""

import os
from dataclasses import dataclass


DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
JSON_SYSTEM_PROMPT = "Respond with exactly one JSON object and nothing else."


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


def complete(config: LLMConfig, prompt: str, *, timeout: int) -> LLMResponse:
    if config.provider == "anthropic":
        return _anthropic_complete(config, prompt, timeout=timeout)
    return _openai_complete(config, prompt, timeout=timeout)


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
