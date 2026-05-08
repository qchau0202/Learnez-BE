"""OpenAI / OpenAI-compatible provider.

Activated when ``LLM_PROVIDER=openai`` and ``OPENAI_API_KEY`` is set,
or via :class:`OpenRouterProvider` when ``LLM_PROVIDER=openrouter``.
Uses the Chat Completions API with native tool calling so we don't need
any extra SDK — plain ``httpx`` is enough.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx

from .base import ChatMessage, ChatProvider, ChatResponse, ToolCall, ToolDefinition

logger = logging.getLogger(__name__)


_OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"


class OpenAIProvider(ChatProvider):
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str | None = None,
        extra_headers: dict[str, str] | None = None,
        provider_name: str = "openai",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._endpoint = (base_url or "https://api.openai.com/v1") + "/chat/completions"
        self._client = httpx.AsyncClient(timeout=30.0)
        self._extra_headers = dict(extra_headers or {})
        # Allow subclasses / OpenRouter to surface a distinct provider
        # name in logs without having to override the whole class.
        self.name = provider_name

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        system_prompt: str,
        tools: list[ToolDefinition],
        extra_body: dict[str, Any] | None = None,
    ) -> ChatResponse:
        wire_messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "tool":
                wire_messages.append(
                    {
                        "role": "tool",
                        "name": m.name or "tool",
                        "tool_call_id": m.tool_call_id or "",
                        "content": m.content,
                    }
                )
                continue
            if m.role == "assistant":
                msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": m.content or None,
                }
                if m.tool_calls:
                    msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments or {}),
                            },
                        }
                        for tc in m.tool_calls
                    ]
                wire_messages.append(msg)
                continue
            wire_messages.append({"role": m.role, "content": m.content})

        body: dict[str, Any] = {
            "model": self._model,
            "messages": wire_messages,
            "temperature": 0.2,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
            body["tool_choice"] = "auto"

        # OpenRouter (and a few OpenAI-compatible vendors) accept extra
        # top-level keys like ``session_id`` for upstream stateful
        # tracking. We forward them verbatim — vendors that don't
        # recognise the keys ignore them, so this is safe to pass even
        # to vanilla OpenAI.
        if extra_body:
            for key, value in extra_body.items():
                if value is None:
                    continue
                body.setdefault(key, value)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }
        resp = await self._client.post(self._endpoint, headers=headers, json=body)
        if resp.status_code >= 400:
            logger.warning(
                "%s API error (model=%s, status=%s): %s",
                self.name,
                self._model,
                resp.status_code,
                resp.text[:500],
            )
            resp.raise_for_status()
        data = resp.json()
        return _parse_openai_response(data)


def _parse_openai_response(data: dict[str, Any]) -> ChatResponse:
    choices = data.get("choices") or []
    if not choices:
        return ChatResponse(content="", raw=data)
    msg = (choices[0] or {}).get("message") or {}
    raw_calls = msg.get("tool_calls") or []
    tool_calls: list[ToolCall] = []
    for c in raw_calls:
        fn = (c or {}).get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        tool_calls.append(
            ToolCall(
                id=str(c.get("id") or f"call_{uuid.uuid4().hex[:8]}"),
                name=str(fn.get("name") or ""),
                arguments=args if isinstance(args, dict) else {},
            )
        )
    return ChatResponse(
        content=str(msg.get("content") or "").strip(),
        tool_calls=tool_calls,
        raw=data,
    )
