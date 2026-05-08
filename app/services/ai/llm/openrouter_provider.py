"""OpenRouter provider with a per-call model cascade.

OpenRouter exposes an OpenAI-compatible Chat Completions API that
aggregates many models behind a single key (Anthropic, OpenAI, Llama,
Qwen, Google, plus several truly free options). We re-use the
:class:`OpenAIProvider` as the transport and just swap the base URL
plus attach OpenRouter's recommended attribution headers.

Model cascade
-------------
On the free tier most ``:free`` models are *upstream* rate-limited
(HTTP 429 from OpenRouter saying ``provider returned error``). To keep
the chatbot responsive we accept a *list* of models — ordered from
"smartest / preferred" to "fastest fallback" — and try them in order
on every chat turn, skipping models that come back 429 / 5xx until one
answers cleanly. The first 4xx that isn't a rate-limit (e.g. 401 bad
key, 400 schema error) propagates immediately so we don't hide bugs.

Configuration
-------------
* ``OPENROUTER_API_KEY`` — required.
* ``OPENROUTER_MODELS`` — comma-separated cascade. First entry wins
  when available, others are tried on transient failure. Falls back to
  ``LLM_MODEL`` (single value) for backwards compatibility.
* ``OPENROUTER_SITE_URL`` / ``OPENROUTER_SITE_TITLE`` — optional
  attribution for the OpenRouter dashboard.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import ChatMessage, ChatResponse, ToolDefinition
from .openai_provider import OpenAIProvider

logger = logging.getLogger(__name__)


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter is OpenAI-compatible — only the URL, headers, and
    the per-call cascade differ."""

    def __init__(
        self,
        *,
        api_key: str,
        models: list[str] | None = None,
        model: str = "openai/gpt-oss-120b:free",
        site_url: str | None = None,
        site_title: str | None = None,
    ) -> None:
        if models:
            cascade = [m.strip() for m in models if m and m.strip()]
        else:
            cascade = [model]
        if not cascade:
            cascade = [model]

        # OpenRouter recommends sending HTTP-Referer + X-Title for free-
        # tier attribution and so app analytics show up in their dash.
        extra: dict[str, str] = {}
        if site_url:
            extra["HTTP-Referer"] = site_url
        if site_title:
            extra["X-Title"] = site_title

        super().__init__(
            api_key=api_key,
            model=cascade[0],
            base_url="https://openrouter.ai/api/v1",
            extra_headers=extra,
            provider_name="openrouter",
        )
        self._cascade: list[str] = cascade

    @property
    def models(self) -> list[str]:
        """Read-only view of the resolved cascade — useful for logs."""
        return list(self._cascade)

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        system_prompt: str,
        tools: list[ToolDefinition],
        extra_body: dict[str, Any] | None = None,
    ) -> ChatResponse:
        """Walk the model cascade until one returns a clean response.

        Skips a model on HTTP 429 or 5xx (the typical "upstream is
        rate-limited" signature on free models) and re-tries with the
        next entry. Anything else propagates so caller-level fallback
        (e.g. Gemini) can take over for genuine outages.

        ``extra_body`` is forwarded to OpenRouter; we use it to pass
        ``session_id`` so OpenRouter can group analytics by chat
        session per their docs.
        """
        last_exc: Exception | None = None
        for idx, candidate in enumerate(self._cascade):
            self._model = candidate
            try:
                resp = await super().generate(
                    messages=messages,
                    system_prompt=system_prompt,
                    tools=tools,
                    extra_body=extra_body,
                )
                if idx > 0:
                    logger.info(
                        "OpenRouter cascade fell through to model %s (#%d).",
                        candidate,
                        idx,
                    )
                return resp
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code if exc.response is not None else 0
                if code == 429 or 500 <= code < 600:
                    logger.warning(
                        "OpenRouter model %s returned %s; trying next in cascade.",
                        candidate,
                        code,
                    )
                    last_exc = exc
                    continue
                raise
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                logger.warning(
                    "OpenRouter model %s network error (%s); trying next.",
                    candidate,
                    type(exc).__name__,
                )
                last_exc = exc
                continue

        # Every model in the cascade failed transiently — re-raise the
        # last error so caller-level fallback can run.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("OpenRouter cascade exhausted with no error captured.")
