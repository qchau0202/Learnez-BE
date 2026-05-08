"""Google Gemini provider implementation.

Activated when ``LLM_PROVIDER=gemini`` and ``GEMINI_API_KEY`` is set.
We use the public Generative Language REST API (no extra SDK dep)
because:

* It's free for low-volume use behind an API key.
* It supports tool / function-calling natively.
* Plain ``httpx`` is already a transitive dep via Supabase, so no
  extra install needed.

The adapter converts our :class:`ChatMessage` / :class:`ToolDefinition`
shape into Gemini's ``contents`` + ``tools`` schema and back, leaving
the agentic runtime in :mod:`app.api.activity.chat` provider-agnostic.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx

from .base import ChatMessage, ChatProvider, ChatResponse, ToolCall, ToolDefinition

logger = logging.getLogger(__name__)


_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


class GeminiProvider(ChatProvider):
    name = "gemini"

    def __init__(self, *, api_key: str, model: str = "gemini-1.5-flash") -> None:
        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(timeout=30.0)

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        system_prompt: str,
        tools: list[ToolDefinition],
    ) -> ChatResponse:
        body: dict[str, Any] = {
            "contents": _to_gemini_contents(messages),
            "systemInstruction": {"parts": [{"text": system_prompt}]},
        }
        if tools:
            body["tools"] = [
                {
                    "function_declarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters,
                        }
                        for t in tools
                    ]
                }
            ]
            # Encourage the model to use a tool when it can — but allow
            # plain text replies for explanations / fallback.
            body["tool_config"] = {"function_calling_config": {"mode": "AUTO"}}

        url = _GEMINI_ENDPOINT.format(model=self._model)
        params = {"key": self._api_key}
        resp = await self._client.post(url, params=params, json=body)
        if resp.status_code >= 400:
            # Surface the API's error body so quota / model-name issues are
            # easy to diagnose from the server log.
            logger.warning(
                "Gemini API error (model=%s, status=%s): %s",
                self._model,
                resp.status_code,
                resp.text[:500],
            )
            resp.raise_for_status()
        data = resp.json()
        return _parse_gemini_response(data)


# --------------------------------------------------------------------------- #
# Wire-format adapters
# --------------------------------------------------------------------------- #


def _to_gemini_contents(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            # System prompt is passed via systemInstruction, not contents.
            continue
        if m.role == "tool":
            # Gemini expects the function response payload as JSON-friendly
            # data — try to parse the runtime's serialised dict back to an
            # object so the model receives structured content.
            response_obj: Any
            try:
                response_obj = json.loads(m.content) if m.content else {}
            except Exception:
                response_obj = {"result": m.content}
            if not isinstance(response_obj, dict):
                response_obj = {"result": response_obj}
            out.append(
                {
                    "role": "function",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": m.name or "tool",
                                "response": response_obj,
                            }
                        }
                    ],
                }
            )
            continue
        if m.role == "assistant":
            parts: list[dict[str, Any]] = []
            if m.content:
                parts.append({"text": m.content})
            for tc in m.tool_calls:
                parts.append(
                    {
                        "functionCall": {
                            "name": tc.name,
                            "args": tc.arguments or {},
                        }
                    }
                )
            if not parts:
                # Empty assistant turn would error; fall back to a noop.
                parts = [{"text": ""}]
            out.append({"role": "model", "parts": parts})
            continue
        out.append({"role": "user", "parts": [{"text": m.content}]})
    return out


def _parse_gemini_response(data: dict[str, Any]) -> ChatResponse:
    candidates = data.get("candidates") or []
    if not candidates:
        return ChatResponse(content="", raw=data)
    parts = (candidates[0].get("content") or {}).get("parts") or []

    tool_calls: list[ToolCall] = []
    text_chunks: list[str] = []
    for part in parts:
        fn = part.get("functionCall")
        if fn:
            tool_calls.append(
                ToolCall(
                    id=f"call_{uuid.uuid4().hex[:8]}",
                    name=str(fn.get("name") or ""),
                    arguments=_coerce_args(fn.get("args")),
                )
            )
        elif "text" in part:
            text_chunks.append(str(part.get("text") or ""))
    return ChatResponse(
        content="".join(text_chunks).strip(),
        tool_calls=tool_calls,
        raw=data,
    )


def _coerce_args(args: Any) -> dict[str, Any]:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            return json.loads(args)
        except Exception:
            return {}
    return {}
