"""Resolve which :class:`ChatProvider` to use at runtime.

Provider selection is driven by environment variables so we can swap
the backing model without touching API code:

* ``LLM_PROVIDER`` — primary provider. One of ``stub`` / ``gemini`` /
  ``openai`` / ``openrouter``. Default is ``stub`` (the offline,
  deterministic provider).
* ``LLM_FALLBACK_PROVIDER`` — optional secondary provider. When set,
  the chat endpoint will retry on the fallback if the primary returns
  a transient failure (quota exceeded, 5xx, etc.). Same value space as
  ``LLM_PROVIDER``; ``stub`` is always available as a last-resort
  fallback even if no value is set.
* ``LLM_MODEL`` — primary provider model id (provider-specific
  default applies if unset).
* ``LLM_FALLBACK_MODEL`` — fallback provider model id.
* ``OPENROUTER_MODELS`` — *cascade* for the OpenRouter primary slot.
  Comma-separated, ordered from "smartest / preferred" to "last
  resort". The OpenRouter provider walks the list per call, skipping
  models that 429/5xx upstream. Falls back to ``LLM_MODEL`` (single
  value) when not set.
* ``OPENROUTER_FALLBACK_MODELS`` — same idea for the fallback slot;
  falls back to ``LLM_FALLBACK_MODEL``.
* Provider-specific keys (any of these can be used by either slot):
    - ``GEMINI_API_KEY`` (or ``GOOGLE_API_KEY``)
    - ``OPENAI_API_KEY``
    - ``OPENROUTER_API_KEY``
* Optional OpenRouter attribution: ``OPENROUTER_SITE_URL`` and
  ``OPENROUTER_SITE_TITLE``.

The factory caches each resolved provider — the underlying provider
holds a long-lived HTTP client.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

from .base import ChatProvider
from .stub_provider import StubProvider

logger = logging.getLogger(__name__)


# Provider-specific defaults — chosen for "cheap and useful" per the
# user's preference. Override via ``LLM_MODEL`` / ``LLM_FALLBACK_MODEL``.
_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"
_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
_DEFAULT_OPENROUTER_MODEL = "openai/gpt-oss-120b:free"


def _build_provider(slug: str, *, model_env: str) -> ChatProvider | None:
    """Instantiate a provider by slug, or return ``None`` if it can't
    be configured (missing key, import error, etc.)."""
    raw = (slug or "").strip().lower()
    if not raw:
        return None
    if raw == "stub":
        return StubProvider()

    model_override = os.getenv(model_env, "").strip() or None

    if raw == "gemini":
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            logger.warning("LLM provider 'gemini' selected but no GEMINI_API_KEY is set.")
            return None
        try:
            from .gemini_provider import GeminiProvider

            return GeminiProvider(
                api_key=api_key,
                model=model_override or _DEFAULT_GEMINI_MODEL,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to init Gemini provider: %s", exc)
            return None

    if raw == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.warning("LLM provider 'openai' selected but no OPENAI_API_KEY is set.")
            return None
        try:
            from .openai_provider import OpenAIProvider

            return OpenAIProvider(
                api_key=api_key,
                model=model_override or _DEFAULT_OPENAI_MODEL,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to init OpenAI provider: %s", exc)
            return None

    if raw == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            logger.warning(
                "LLM provider 'openrouter' selected but no OPENROUTER_API_KEY is set."
            )
            return None
        # The OpenRouter cascade is slot-aware: primary uses
        # OPENROUTER_MODELS, fallback uses OPENROUTER_FALLBACK_MODELS.
        # When neither cascade var is set we still honour the single-
        # value LLM_MODEL / LLM_FALLBACK_MODEL for backwards compat.
        if model_env == "LLM_MODEL":
            cascade_env = "OPENROUTER_MODELS"
        else:
            cascade_env = "OPENROUTER_FALLBACK_MODELS"
        cascade_raw = os.getenv(cascade_env, "").strip()
        cascade = (
            [m.strip() for m in cascade_raw.split(",") if m.strip()]
            if cascade_raw
            else []
        )
        if not cascade:
            cascade = [model_override or _DEFAULT_OPENROUTER_MODEL]
        try:
            from .openrouter_provider import OpenRouterProvider

            provider = OpenRouterProvider(
                api_key=api_key,
                models=cascade,
                site_url=os.getenv("OPENROUTER_SITE_URL", "").strip() or None,
                site_title=os.getenv("OPENROUTER_SITE_TITLE", "").strip() or None,
            )
            logger.info(
                "OpenRouter cascade (%s slot): %s",
                "primary" if model_env == "LLM_MODEL" else "fallback",
                ", ".join(cascade),
            )
            return provider
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to init OpenRouter provider: %s", exc)
            return None

    logger.warning("Unknown LLM provider slug: %r", slug)
    return None


@lru_cache(maxsize=1)
def get_provider() -> ChatProvider:
    """Resolve the configured primary provider, falling back to the
    deterministic stub if nothing is wired up."""
    return (
        _build_provider(os.getenv("LLM_PROVIDER", "stub"), model_env="LLM_MODEL")
        or StubProvider()
    )


@lru_cache(maxsize=1)
def get_fallback_provider() -> ChatProvider | None:
    """Resolve the optional fallback provider used when the primary
    fails with a transient error (quota / 5xx). Returns ``None`` when
    ``LLM_FALLBACK_PROVIDER`` is not set so the chat runtime can decide
    whether to surface the error or degrade silently to stub."""
    slug = os.getenv("LLM_FALLBACK_PROVIDER", "").strip()
    if not slug:
        return None
    return _build_provider(slug, model_env="LLM_FALLBACK_MODEL")
