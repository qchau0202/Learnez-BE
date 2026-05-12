"""LLM provider abstraction for the agentic learning-path chatbot.

This is the single source of truth for everything LLM-related on the
backend. It bundles:

* The provider-agnostic types (:class:`ChatProvider`,
  :class:`ChatMessage`, :class:`ToolCall`, :class:`ToolDefinition`,
  :class:`ChatResponse`).
* Concrete providers — :class:`StubProvider` (offline, deterministic),
  :class:`GeminiProvider`, :class:`OpenAIProvider`,
  :class:`OpenRouterProvider`.
* The :func:`get_provider` / :func:`get_fallback_provider` factory
  functions used by ``app/api/activity/chat.py``.

The split into one module (down from 7 files) makes "where do I tweak
the LLM?" trivial to answer and keeps the codebase tidy. Adding a new
provider stays straightforward: drop a new ``Provider`` class below
and register it in :func:`_build_provider`.

Environment variables
---------------------
``LLM_PROVIDER`` — primary provider slug: ``stub`` / ``gemini`` /
``openai`` / ``openrouter``. Default ``stub`` (offline, no API key
needed).

``LLM_FALLBACK_PROVIDER`` — optional secondary provider. Used when
the primary returns a transient failure (quota exceeded, 5xx). Same
value space as ``LLM_PROVIDER``.

``LLM_MODEL`` / ``LLM_FALLBACK_MODEL`` — primary / fallback model id.
Provider-specific default applies if unset.

``OPENROUTER_MODELS`` / ``OPENROUTER_FALLBACK_MODELS`` — cascade list
(comma-separated) for the OpenRouter slots. Useful on the free tier
where individual models 429 a lot.

Provider keys (read on demand):
* ``GEMINI_API_KEY`` (or ``GOOGLE_API_KEY``)
* ``OPENAI_API_KEY``
* ``OPENROUTER_API_KEY``

Optional OpenRouter attribution: ``OPENROUTER_SITE_URL`` and
``OPENROUTER_SITE_TITLE``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal, Protocol

import httpx

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Provider-agnostic types
# --------------------------------------------------------------------------- #


# Roles we accept on the wire. ``tool`` represents the result of a
# tool call being fed back into the model on the next turn.
ChatRole = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    """A single tool invocation requested by the LLM."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatMessage:
    role: ChatRole
    content: str
    name: str | None = None  # tool name when role == "tool"
    tool_call_id: str | None = None
    # When role == "assistant" and this turn was a tool-call response
    # from the model, populate ``tool_calls`` so providers can replay
    # the canonical functionCall / functionResponse pair on the next
    # round.
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ToolDefinition:
    """One callable surface exposed to the LLM.

    ``parameters`` follows JSON Schema conventions so it works with
    both Gemini's ``function_declarations`` and OpenAI's ``functions``
    / ``tools`` formats with minimal adaptation.
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ChatResponse:
    """What every provider must return after one inference round."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] | None = None


class ChatProvider(Protocol):
    """Minimal interface the agentic runtime depends on."""

    name: str

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        system_prompt: str,
        tools: list[ToolDefinition],
    ) -> ChatResponse:
        """Run one inference round and return either content or tool calls."""
        ...


# --------------------------------------------------------------------------- #
# Stub provider — offline, deterministic, no network
# --------------------------------------------------------------------------- #


_COURSE_CODE_RE = re.compile(r"\b([A-Za-z]{2,4})\s*-?\s*(\d{2,4})\b")
_POSITION_RE = re.compile(
    r"(?:position|slot|step|spot)\s*#?\s*(\d+)|to\s+(\d+)\b",
    re.IGNORECASE,
)
_SWAP_RE = re.compile(
    r"(?:swap|replace|exchange|change)\s+([A-Za-z]{2,4}\s*-?\s*\d{2,4})\s+(?:for|with|to)\s+([A-Za-z]{2,4}\s*-?\s*\d{2,4})",
    re.IGNORECASE,
)


def _extract_course_code(text: str) -> str | None:
    m = _COURSE_CODE_RE.search(text)
    if not m:
        return None
    return f"{m.group(1).upper()}{m.group(2)}"


def _extract_all_codes(text: str) -> list[str]:
    return [
        f"{m.group(1).upper()}{m.group(2)}"
        for m in _COURSE_CODE_RE.finditer(text)
    ]


def _maybe_reorder_intent(text: str) -> dict[str, Any] | None:
    """Return the args for a ``reorder_path`` tool call, or ``None``."""
    lower = text.lower()
    if not any(verb in lower for verb in ("move", "put", "place", "shift", "reorder")):
        return None
    code = _extract_course_code(text)
    if not code:
        return None
    if any(kw in lower for kw in ("first", "front", "top", "beginning", "start")):
        return {"course_code": code, "to_position": 1}
    if any(kw in lower for kw in ("last", "end", "bottom", "back")):
        return {"course_code": code, "to_position": -1}
    pos = _POSITION_RE.search(text)
    if pos:
        n = int(pos.group(1) or pos.group(2))
        return {"course_code": code, "to_position": max(1, n)}
    return None


def _maybe_swap_intent(text: str) -> dict[str, Any] | None:
    m = _SWAP_RE.search(text)
    if m:
        a = m.group(1).upper().replace(" ", "").replace("-", "")
        b = m.group(2).upper().replace(" ", "").replace("-", "")
        return {"course_code": a, "alternative_code": b}
    if "swap" in text.lower():
        codes = _extract_all_codes(text)
        if len(codes) >= 2:
            return {"course_code": codes[0], "alternative_code": codes[1]}
    return None


def _extract_major(text: str) -> str | None:
    upper = text.upper()
    aliases = {
        "SE": "Software Engineering",
        "CS": "Computer Science",
        "BA": "Business Administration",
        "FIN": "Finance",
        "SOFTWARE ENGINEERING": "Software Engineering",
        "COMPUTER SCIENCE": "Computer Science",
        "BUSINESS": "Business Administration",
        "FINANCE": "Finance",
    }
    for key, val in aliases.items():
        if key in upper:
            return val
    return None


def _tool_call_response(name: str, arguments: dict[str, Any]) -> ChatResponse:
    return ChatResponse(
        tool_calls=[
            ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=name, arguments=arguments)
        ]
    )


def _summarise_tool_result(tool_msg: ChatMessage) -> str:
    """Stub closing turn after a tool has been executed."""
    name = tool_msg.name or "tool"
    if name == "navigate":
        return "Done — switching analytics tabs now."
    if name == "reorder_path":
        return "Done — your path has been reordered."
    if name == "apply_alternative":
        return "Done — swapped the slot in your path."
    if name == "explain_recommendation":
        content = tool_msg.content or ""
        if "explanation" in content:
            return (
                "Here's why that course is flagged — see the explanation above. "
                "Remember the path is just guidance; you decide what to act on."
            )
        return "Here's the reasoning for that flag based on your latest grades."
    if name == "build_initial_path":
        return (
            "Drafted a starter path from the curriculum graph. These are "
            "recommendations — feel free to skip or reorder anything that "
            "doesn't match your goals."
        )
    if name == "summarize_recent_grades":
        return (
            "Here's where things stand based on your graded work. Use the "
            "remedial flags as a nudge — nothing is mandatory."
        )
    return "Done."


class StubProvider(ChatProvider):
    """Deterministic stub provider used when no API key is configured.

    Keeps the agentic chat endpoint fully usable offline and during
    development. Mirrors the canonical tool-calling shape so the
    runtime behaves exactly like a real provider — only the answers
    are simpler and rule-based.
    """

    name = "stub"

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        system_prompt: str,
        tools: list[ToolDefinition],
    ) -> ChatResponse:
        # If the last message is a tool result, we've already executed
        # the action — produce a short prose confirmation and stop the
        # agentic loop instead of re-parsing the user input.
        if messages and messages[-1].role == "tool":
            return ChatResponse(content=_summarise_tool_result(messages[-1]))

        last_user = next((m for m in reversed(messages) if m.role == "user"), None)
        text = (last_user.content or "").strip() if last_user else ""
        lower = text.lower()
        tool_names = {t.name for t in tools}

        if not text:
            return ChatResponse(
                content=(
                    "Hi! I'm your AI Learning Assistant. Ask me to reorder your "
                    "path, swap a course, explain a recommendation, or jump to "
                    "another analytics tab."
                )
            )

        # Navigation intents
        nav_targets = [
            ("overview", "/analytics?tab=overview"),
            ("behavior", "/analytics?tab=behavior"),
            ("learning path", "/analytics?tab=learning-path"),
            ("risk", "/analytics?tab=dropout"),
            ("dropout", "/analytics?tab=dropout"),
        ]
        if "navigate" in tool_names or "go_to" in tool_names:
            for keyword, target in nav_targets:
                if keyword in lower and any(
                    verb in lower for verb in ("show", "open", "go", "take", "navigate", "switch")
                ):
                    return _tool_call_response(
                        "navigate",
                        {"path": target, "reason": f"Opening {keyword.title()} tab as requested."},
                    )

        # Reorder
        if "reorder_path" in tool_names:
            reorder = _maybe_reorder_intent(text)
            if reorder is not None:
                return _tool_call_response("reorder_path", reorder)

        # Swap / apply alternative
        if "apply_alternative" in tool_names:
            swap = _maybe_swap_intent(text)
            if swap is not None:
                return _tool_call_response("apply_alternative", swap)

        # Explain
        if "explain_recommendation" in tool_names:
            code = _extract_course_code(text)
            if code and ("why" in lower or "explain" in lower or "what" in lower):
                return _tool_call_response("explain_recommendation", {"course_code": code})

        # Summarise grades
        if "summarize_recent_grades" in tool_names:
            grade_keywords = (
                "how am i doing",
                "how am i performing",
                "recent grades",
                "grade trend",
                "grade summary",
                "doing in",
                "performance in",
                "trend",
                "summary of my grades",
            )
            if any(kw in lower for kw in grade_keywords):
                code = _extract_course_code(text)
                args: dict[str, Any] = {}
                if code:
                    args["course_code"] = code
                return _tool_call_response("summarize_recent_grades", args)

        # New-student onboarding
        if "build_initial_path" in tool_names:
            if (
                "new" in lower
                and ("student" in lower or "starter" in lower or "beginner" in lower)
            ) or "build me" in lower or "starter path" in lower:
                major = _extract_major(text)
                if major:
                    return _tool_call_response(
                        "build_initial_path", {"major": major}
                    )

        # Fallback: structured help text rather than wild generation.
        return ChatResponse(
            content=(
                "I can help you with a few things in your analytics view:\n"
                "• \"Move IT404 to position 1\" — reorder your path\n"
                "• \"Swap IT404 for IT220\" — apply a catalog alternative\n"
                "• \"Why is IT404 remedial?\" — explain a recommendation\n"
                "• \"Open my behavior tab\" — navigate inside analytics\n"
                "• \"I'm a new SE student, build me a starter path\" — onboard\n\n"
                "I'm scoped to analytics only and won't change course data."
            )
        )


# --------------------------------------------------------------------------- #
# Gemini provider
# --------------------------------------------------------------------------- #


_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


def _to_gemini_contents(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            # System prompt goes via ``systemInstruction``, not contents.
            continue
        if m.role == "tool":
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
            args_obj = fn.get("args")
            if isinstance(args_obj, str):
                try:
                    args_obj = json.loads(args_obj)
                except Exception:
                    args_obj = {}
            if not isinstance(args_obj, dict):
                args_obj = {}
            tool_calls.append(
                ToolCall(
                    id=f"call_{uuid.uuid4().hex[:8]}",
                    name=str(fn.get("name") or ""),
                    arguments=args_obj,
                )
            )
        elif "text" in part:
            text_chunks.append(str(part.get("text") or ""))
    return ChatResponse(
        content="".join(text_chunks).strip(),
        tool_calls=tool_calls,
        raw=data,
    )


class GeminiProvider(ChatProvider):
    """Google Gemini, via the public Generative Language REST API.

    No extra SDK needed — plain ``httpx`` keeps the dep tree narrow.
    """

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
            body["tool_config"] = {"function_calling_config": {"mode": "AUTO"}}

        url = _GEMINI_ENDPOINT.format(model=self._model)
        params = {"key": self._api_key}
        resp = await self._client.post(url, params=params, json=body)
        if resp.status_code >= 400:
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
# OpenAI provider (also used as transport for OpenRouter)
# --------------------------------------------------------------------------- #


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


class OpenAIProvider(ChatProvider):
    """OpenAI Chat Completions provider (also the transport for OpenRouter)."""

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
        # Allow subclasses to surface a distinct provider name in logs
        # without having to override the whole class.
        self.name = provider_name

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        system_prompt: str,
        tools: list[ToolDefinition],
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


# --------------------------------------------------------------------------- #
# OpenRouter provider — OpenAI-compatible with a per-call model cascade
# --------------------------------------------------------------------------- #


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter, OpenAI-compatible — only the URL, headers, and the
    per-call model cascade differ.

    Free-tier models 429 a lot, so we accept an ordered list of model
    ids and walk it per call, skipping ones that fail transiently
    until something answers. The first 4xx that isn't a rate-limit
    propagates immediately so we don't hide real bugs.
    """

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

        # OpenRouter recommends sending HTTP-Referer + X-Title for
        # free-tier attribution.
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
    ) -> ChatResponse:
        last_exc: Exception | None = None
        for idx, candidate in enumerate(self._cascade):
            self._model = candidate
            try:
                resp = await super().generate(
                    messages=messages,
                    system_prompt=system_prompt,
                    tools=tools,
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


# --------------------------------------------------------------------------- #
# Factory — pick the right provider based on env vars
# --------------------------------------------------------------------------- #


# Provider-specific defaults — "cheap and useful". Override via
# ``LLM_MODEL`` / ``LLM_FALLBACK_MODEL``.
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
        # Primary slot reads OPENROUTER_MODELS; fallback slot reads
        # OPENROUTER_FALLBACK_MODELS. When neither cascade var is set
        # we still honour the single-value LLM_MODEL / LLM_FALLBACK_MODEL
        # for backwards compatibility.
        cascade_env = (
            "OPENROUTER_MODELS"
            if model_env == "LLM_MODEL"
            else "OPENROUTER_FALLBACK_MODELS"
        )
        cascade_raw = os.getenv(cascade_env, "").strip()
        cascade = (
            [m.strip() for m in cascade_raw.split(",") if m.strip()]
            if cascade_raw
            else []
        )
        if not cascade:
            cascade = [model_override or _DEFAULT_OPENROUTER_MODEL]
        try:
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
    ``LLM_FALLBACK_PROVIDER`` is not set so the chat runtime can
    decide whether to surface the error or degrade silently to stub.
    """
    slug = os.getenv("LLM_FALLBACK_PROVIDER", "").strip()
    if not slug:
        return None
    return _build_provider(slug, model_env="LLM_FALLBACK_MODEL")


__all__ = [
    "ChatMessage",
    "ChatProvider",
    "ChatResponse",
    "ChatRole",
    "GeminiProvider",
    "OpenAIProvider",
    "OpenRouterProvider",
    "StubProvider",
    "ToolCall",
    "ToolDefinition",
    "get_fallback_provider",
    "get_provider",
]
