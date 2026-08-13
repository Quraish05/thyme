"""Shared provider plumbing for the AI features.

Every AI feature in this app — follow-up extraction, tag suggestion, the journal
Q&A, the goal evaluator, and more — talks to the same two providers (Anthropic and
Gemini) with the same strict, schema-constrained structured output. This module
holds the parts that don't change between features: client construction, provider
selection, the actual API call, and the shared :class:`AIResult` result base. Each
feature supplies its own prompt and output schema on top.

The structured-output contract is provider-specific in shape but identical in
intent (CCAF Domain 4.3): Anthropic takes a raw JSON Schema dict via
``output_config``; Gemini takes a Pydantic model as ``response_schema`` and
converts it (Optional -> nullable, Literal -> enum). Callers pass both.
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import TypeVar

from anthropic import APIError, AsyncAnthropic, AuthenticationError, PermissionDeniedError
from google import genai
from google.genai import errors as gemini_errors
from google.genai import types as gemini_types
from pydantic import BaseModel, ValidationError

from app.core.config import settings

logger = logging.getLogger(__name__)

# A Pydantic model type the structured helper validates into and returns.
_Model = TypeVar("_Model", bound=BaseModel)

# First attempt plus validation-retries (CCAF 4.4). Kept tiny on purpose —
# retries are a correction path, not a primary stopping mechanism.
_DEFAULT_MAX_ATTEMPTS = 2

# Model used when AI_MODEL is left blank, per provider. Both support the strict
# JSON structured output these features rely on. Gemini's flash tier is free.
DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5",
    # "-latest" alias tracks the current free flash model, so it doesn't 404 as
    # Google rotates versions. Pin a specific one via AI_MODEL for stable behavior.
    "gemini": "gemini-flash-latest",
}


class AIError(RuntimeError):
    """A call to the AI provider could not produce a usable result."""


class AINotConfiguredError(AIError):
    """AI features are disabled because the active provider's key is missing/rejected."""


@dataclass
class AIResult:
    """Base for an AI feature's result: which model answered, and whether it ran.

    Every paid AI feature that can *short-circuit* (return a canned answer without
    calling the provider — e.g. an empty journal, or text too thin to tag) shares
    this shape. Subclasses add their own payload field(s). The route charges a
    credit only when ``used_model`` is True, so a short-circuited call is free.
    """

    model: str
    used_model: bool


@lru_cache(maxsize=1)
def _anthropic_client() -> AsyncAnthropic:
    """The Anthropic async client, built once from the configured key."""
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


def anthropic_client() -> AsyncAnthropic:
    """The shared async Anthropic client — for the streaming/tool-use chat feature.

    Structured features go through ``generate_structured``; the chat assistant
    drives its own streaming tool loop and needs the raw client, so expose it.
    """
    return _anthropic_client()


@lru_cache(maxsize=1)
def _gemini_client() -> genai.Client:
    """The Gemini (Google AI Studio) client, built once from the configured key."""
    return genai.Client(api_key=settings.gemini_api_key)


def _provider_key(provider: str) -> str:
    """The API key for the active provider ("" when unset)."""
    return settings.gemini_api_key if provider == "gemini" else settings.anthropic_api_key


def active_model() -> str:
    """The model name that would be used, without requiring a key.

    For the transparency field on empty/short-circuited results, where we skip
    the API entirely and so shouldn't demand configuration to name the model.
    """
    return settings.ai_model or DEFAULT_MODELS.get(settings.ai_provider, "")


def resolve_provider_and_model() -> tuple[str, str]:
    """The active provider and the model to use, or raise if unconfigured.

    Raises :class:`AINotConfiguredError` when the configured provider has no key,
    so features fail loudly with a setup message rather than a cryptic API error.
    """
    provider = settings.ai_provider
    if not _provider_key(provider):
        raise AINotConfiguredError(
            f"AI features are not configured (no API key for provider '{provider}')."
        )
    return provider, active_model()


def count_words(*parts: str) -> int:
    """Whitespace word count across the given text parts — the thin-content gate."""
    return len(" ".join(parts).split())


def _first_text(response) -> str:
    """The first text block of an Anthropic response (schema guarantees one)."""
    return next((block.text for block in response.content if block.type == "text"), "")


async def _generate_anthropic(system: str, turns: list[dict], model: str, schema: dict) -> str:
    """Call Claude with strict structured output; return the raw JSON text."""
    try:
        response = await _anthropic_client().messages.create(
            model=model,
            max_tokens=settings.ai_max_output_tokens,
            system=system,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=turns,
        )
    except (AuthenticationError, PermissionDeniedError) as exc:
        raise AINotConfiguredError(
            "The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in the backend .env."
        ) from exc
    except APIError as exc:
        raise AIError(f"Anthropic API error: {exc}") from exc
    return _first_text(response)


async def _generate_gemini(system: str, turns: list[dict], model: str, response_schema) -> str:
    """Call Gemini with structured output; return the raw JSON text."""
    # Gemini uses roles "user"/"model" (not "assistant") and a parts list.
    contents = [
        {
            "role": "model" if turn["role"] == "assistant" else "user",
            "parts": [{"text": turn["content"]}],
        }
        for turn in turns
    ]
    try:
        response = await _gemini_client().aio.models.generate_content(
            model=model,
            contents=contents,
            config=gemini_types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=response_schema,
                max_output_tokens=settings.ai_max_output_tokens,
            ),
        )
    except gemini_errors.ClientError as exc:
        # A rejected key surfaces as 400 "API key not valid" / 401 / 403 — a
        # setup problem, not a transient one.
        message = str(exc).lower()
        if getattr(exc, "code", None) in (401, 403) or "api key" in message or "api_key" in message:
            raise AINotConfiguredError(
                "The Gemini API key was rejected. Check GEMINI_API_KEY in the backend .env."
            ) from exc
        raise AIError(f"Gemini API error: {exc}") from exc
    except gemini_errors.APIError as exc:
        raise AIError(f"Gemini API error: {exc}") from exc
    return response.text or ""


async def generate(
    *,
    system: str,
    turns: list[dict],
    provider: str,
    model: str,
    anthropic_schema: dict,
    gemini_schema,
) -> str:
    """Dispatch to the active provider and return the raw JSON text.

    ``anthropic_schema`` is a raw JSON Schema dict; ``gemini_schema`` is the
    Pydantic model to constrain Gemini's output — the same contract expressed two
    ways, because the two SDKs accept different forms.
    """
    if provider == "gemini":
        return await _generate_gemini(system, turns, model, gemini_schema)
    return await _generate_anthropic(system, turns, model, anthropic_schema)


async def generate_structured(
    *,
    system: str,
    user_message: str,
    anthropic_schema: dict,
    response_model: type[_Model],
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> tuple[_Model, str]:
    """Generate one schema-constrained result, validated into ``response_model``.

    This is the CCAF 4.3 + 4.4 core shared by every AI note feature: resolve the
    provider, call the model with strict structured output, and validate the JSON
    into the given Pydantic model. On a validation failure it retries — up to
    ``max_attempts`` total — feeding the model back its bad output and the error,
    then gives up rather than looping forever. Callers supply only their prompt
    and schema; the mechanism lives here once.

    Returns the validated instance and the resolved model name. Raises
    :class:`AINotConfiguredError` when unconfigured, or :class:`AIError` when the
    model never returns a valid result.
    """
    provider, model = resolve_provider_and_model()
    turns: list[dict] = [{"role": "user", "content": user_message}]

    last_error: Exception | None = None
    for attempt in range(max_attempts):
        raw = await generate(
            system=system,
            turns=turns,
            provider=provider,
            model=model,
            anthropic_schema=anthropic_schema,
            gemini_schema=response_model,
        )
        try:
            return response_model.model_validate_json(raw), model
        except ValidationError as exc:
            last_error = exc
            logger.warning(
                "Structured generation failed validation (attempt %s): %s", attempt + 1, exc
            )
            # Retry-with-error-feedback: show the model its bad output and the
            # specific error, and ask for a corrected response.
            turns.append({"role": "assistant", "content": raw})
            turns.append(
                {
                    "role": "user",
                    "content": (
                        f"That response failed validation:\n{exc}\n\n"
                        "Return a corrected response using the JSON schema."
                    ),
                }
            )

    raise AIError(
        "Model did not return a valid structured response after retries."
    ) from last_error
