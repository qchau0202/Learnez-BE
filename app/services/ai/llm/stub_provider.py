"""Deterministic stub provider used when no API key is configured.

This keeps the agentic chat endpoint *fully usable* offline and during
development:

* It never hits the network.
* It mirrors the canonical tool-calling shape so the runtime has the
  same behaviour as a real provider — only the answers are simpler
  and rule-based.
* It speaks a small, hard-coded "intent grammar" tuned to the chatbot
  scope the user described:

    - **Reorder path** ("move IT404 to the front", "put X first")
    - **Apply alternative** ("swap IT404 for IT220")
    - **Navigate** ("show me my behavior tab", "open risk analysis")
    - **Explain recommendation** ("why IT404 is remedial")
    - **Initial path for new students** ("I'm a new SE student",
      "build me a starter path")

Replace with a real LLM provider once we have an API key. The output
format (free-form ``content`` *or* a list of ``tool_calls``) is the
same.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from .base import ChatMessage, ChatProvider, ChatResponse, ToolCall, ToolDefinition


class StubProvider(ChatProvider):
    name = "stub"

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        system_prompt: str,
        tools: list[ToolDefinition],
        extra_body: dict[str, Any] | None = None,
    ) -> ChatResponse:
        # The stub is offline, so any provider-specific body keys
        # (e.g. OpenRouter's ``session_id``) are simply discarded.
        del extra_body
        # If the last message is a tool result, we've already executed
        # the action — produce a short prose confirmation and stop the
        # agentic loop instead of re-parsing the user input and looping
        # forever.
        if messages and messages[-1].role == "tool":
            return ChatResponse(content=_summarise_tool_result(messages[-1]))

        # Find the most recent user message; that's the only thing we
        # interpret. Earlier turns are ignored deliberately to keep this
        # provider stateless and predictable.
        last_user = next(
            (m for m in reversed(messages) if m.role == "user"), None
        )
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

        # ------------------------------------------------------------------
        # 1. Navigation intents (analytics tabs only — by design we never
        #    expose course CRUD here)
        # ------------------------------------------------------------------
        nav_targets: list[tuple[str, str]] = [
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

        # ------------------------------------------------------------------
        # 2. Reorder ("move X to position Y", "put X first/last")
        # ------------------------------------------------------------------
        if "reorder_path" in tool_names:
            reorder = _maybe_reorder_intent(text)
            if reorder is not None:
                return _tool_call_response("reorder_path", reorder)

        # ------------------------------------------------------------------
        # 3. Swap / apply alternative ("swap IT404 for IT220")
        # ------------------------------------------------------------------
        if "apply_alternative" in tool_names:
            swap = _maybe_swap_intent(text)
            if swap is not None:
                return _tool_call_response("apply_alternative", swap)

        # ------------------------------------------------------------------
        # 4. Explain a course / recommendation ("why is IT404 remedial?")
        # ------------------------------------------------------------------
        if "explain_recommendation" in tool_names:
            code = _extract_course_code(text)
            if code and ("why" in lower or "explain" in lower or "what" in lower):
                return _tool_call_response("explain_recommendation", {"course_code": code})

        # ------------------------------------------------------------------
        # 4b. Summarise recent grades ("how am I doing in IT404?",
        #     "show my recent grades", "trend for IT220")
        # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # 5. New-student onboarding ("I'm a new SE student", "starter path")
        # ------------------------------------------------------------------
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
# Intent parsing helpers — small and deliberately narrow.
# --------------------------------------------------------------------------- #


_COURSE_CODE_RE = re.compile(r"\b([A-Za-z]{2,4})\s*-?\s*(\d{2,4})\b")


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


_POSITION_RE = re.compile(
    r"(?:position|slot|step|spot)\s*#?\s*(\d+)|to\s+(\d+)\b",
    re.IGNORECASE,
)


def _maybe_reorder_intent(text: str) -> dict[str, Any] | None:
    """Return the args for a ``reorder_path`` tool call, or ``None``.

    Recognised forms:
    * "Move IT404 to position 1"
    * "Put IT220 first"
    * "Move IT404 to the end" / "...to the bottom"
    """
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


_SWAP_RE = re.compile(
    r"(?:swap|replace|exchange|change)\s+([A-Za-z]{2,4}\s*-?\s*\d{2,4})\s+(?:for|with|to)\s+([A-Za-z]{2,4}\s*-?\s*\d{2,4})",
    re.IGNORECASE,
)


def _maybe_swap_intent(text: str) -> dict[str, Any] | None:
    m = _SWAP_RE.search(text)
    if m:
        a = m.group(1).upper().replace(" ", "").replace("-", "")
        b = m.group(2).upper().replace(" ", "").replace("-", "")
        return {"course_code": a, "alternative_code": b}
    # Fallback: any sentence with two course codes + the word "swap"
    if "swap" in text.lower():
        codes = _extract_all_codes(text)
        if len(codes) >= 2:
            return {"course_code": codes[0], "alternative_code": codes[1]}
    return None


def _extract_major(text: str) -> str | None:
    """Pull a major name out of a free-form sentence.

    Right now we accept either the abbreviation (CS, SE, BA, FIN) or a
    spelled-out form. Real provider can do better; the stub stays
    boring on purpose.
    """
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
    """Stub closing turn after a tool has been executed.

    A real LLM would generate context-aware prose here. The stub keeps
    things short and uses the tool name + result string so the user
    always gets a confirmation. We deliberately avoid trying to parse
    ``tool_msg.content`` — it's a plain ``str(dict)`` from the runtime
    and re-parsing it is brittle.
    """
    name = tool_msg.name or "tool"
    if name == "navigate":
        return "Done — switching analytics tabs now."
    if name == "reorder_path":
        return "Done — your path has been reordered."
    if name == "apply_alternative":
        return "Done — swapped the slot in your path."
    if name == "explain_recommendation":
        # Pull the explanation out if we can, otherwise stay neutral.
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
