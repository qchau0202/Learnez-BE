"""Agentic chatbot for the student analytics view.

This is the runtime that ties together:

* The pluggable :mod:`app.services.ai.llm` provider (stub by default,
  OpenRouter / Gemini / OpenAI when an API key is wired up).
* The analytics-only tool registry in
  :mod:`app.services.ai.chat_tools` — the bot can reorder the path,
  swap alternatives, navigate analytics tabs, explain recommendations,
  and build a starter path for new students. It cannot touch course
  data.
* Per-student chat **sessions** persisted to MongoDB
  (:mod:`app.services.ai.chat_sessions`):

    - ``learnez_ai.agent_runs`` — session metadata.
    - ``elearning_raw.chat_events`` — turn log (user + assistant turns).
    - ``elearning_raw.ai_action_events`` — tool-call audit trail.

The student-facing UI gives every user up to **five active sessions**.
Beyond the cap they must delete one before starting a new conversation.
The cap is enforced server-side in
:func:`chat_sessions.enforce_session_cap`.

Agent loop
----------
1. Frontend posts the running history *plus* a ``session_id``.
2. We append a fresh system prompt that pins down scope.
3. We run a bounded multi-turn loop: at most three LLM rounds + six
   tool calls per user turn so a misbehaving model cannot run away.
4. Every tool call is dispatched locally; we audit it in
   ``ai_action_events``.
5. The user message and the assistant turns are persisted to
   ``chat_events`` so the FE can hydrate any session on demand.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.dependencies import ROLE_MAP
from app.services.ai import chat_sessions
from app.services.ai.chat_tools import (
    dispatch_tool,
    list_tool_definitions,
)
from app.services.ai.llm import (
    ChatMessage,
    ChatProvider,
    ChatResponse,
    get_fallback_provider,
    get_provider,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["AI Chat"])


_SYSTEM_PROMPT = (
    "You are LearnEZ's AI Learning Assistant, embedded in the student "
    "Analytics view. The user you are chatting with is a logged-in "
    "student looking at their own analytics. You help them reflect on "
    "their learning path, behaviour, and dropout-risk insights.\n\n"
    "Hard rules — do not violate:\n"
    "1. You may ONLY perform analytics-side actions through the "
    "registered tools (reorder_path, apply_alternative, navigate, "
    "explain_recommendation, summarize_recent_grades, "
    "build_initial_path).\n"
    "2. You MUST NOT create, edit, delete, or mutate course data, "
    "enrolments, grades, assignments, or any administrative records. "
    "If a user asks for that, politely refuse and suggest contacting "
    "an admin or lecturer.\n"
    "3. Navigation is restricted to the analytics tabs whitelisted by "
    "the navigate tool.\n"
    "4. The learning path is a recommendation, not a registration "
    "plan — frame all suggestions as ideas the student can choose to "
    "act on.\n"
    "5. For new students with no academic history, use "
    "build_initial_path with the curriculum graph; for progressing "
    "students, work from their existing path and grade signals.\n"
    "6. AGENT LOOP: you can chain up to 3 tool rounds per user turn. "
    "Use this to *discover then act*: e.g. for 'move my cybersecurity "
    "course to slot 1', first call summarize_recent_grades (no args) "
    "to find the matching course_code, then in the next round call "
    "reorder_path with the real code. After all tools have run, your "
    "FINAL message must be 1-2 sentences of natural-language summary "
    "for the student. If you call a tool and feel certain about the "
    "narration, you can include prose in the same round — but the "
    "FINAL round (when you stop calling tools) MUST contain prose.\n"
    "7. NEVER invent course codes. The catalog uses real codes like "
    "IT404, BA303, FIN101 — they come from the student's actual "
    "enrolments. If the user mentions a course by topic name only "
    "('cyber security', 'marketing', 'algorithms'): FIRST call "
    "summarize_recent_grades with NO course_code argument to get the "
    "list of their enrolled courses (each entry has course_code + "
    "course_title), THEN reason over that list to pick the best match "
    "and call the relevant tool with the real code. If nothing in the "
    "list matches, say so plainly — do not guess a code.\n\n"
    "Position semantics: ``reorder_path.to_position`` is 1-indexed "
    "(1 = first slot). Use -1 to send a course to the end.\n\n"
    "Style: short, concrete, encouraging."
)


# --------------------------------------------------------------------------- #
# Pydantic wire types
# --------------------------------------------------------------------------- #


class ChatRequestMessage(BaseModel):
    role: str = Field(pattern=r"^(user|assistant)$")
    content: str = Field(min_length=1, max_length=4000)


class ChatRequestBody(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    messages: list[ChatRequestMessage] = Field(min_length=1, max_length=40)


class ChatToolCallResult(BaseModel):
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class ChatResponseBody(BaseModel):
    reply: str
    provider: str
    session_id: str
    tool_results: list[ChatToolCallResult] = Field(default_factory=list)


class ChatSessionView(BaseModel):
    session_id: str
    title: str
    status: str
    message_count: int
    created_at: str | None = None
    updated_at: str | None = None


class ChatSessionList(BaseModel):
    sessions: list[ChatSessionView]
    max_sessions: int


class CreateSessionBody(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ChatSessionMessage(BaseModel):
    turn_id: str
    role: str
    content: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    event_time: str | None = None
    provider: str | None = None


class ChatSessionMessageList(BaseModel):
    session_id: str
    messages: list[ChatSessionMessage]


# --------------------------------------------------------------------------- #
# Auth helper
# --------------------------------------------------------------------------- #


def _require_student(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthenticated")
    role_name = ROLE_MAP.get(user.get("role_id"))
    if role_name != "Student":
        raise HTTPException(status_code=403, detail="Student-only endpoint")
    student_id = str(user.get("user_id") or "").strip()
    if not student_id:
        raise HTTPException(status_code=401, detail="Unauthenticated")
    return student_id


# --------------------------------------------------------------------------- #
# Session CRUD endpoints
# --------------------------------------------------------------------------- #


@router.get("/sessions", response_model=ChatSessionList)
async def list_chat_sessions(request: Request):
    """List the student's active chat sessions, newest first."""
    student_id = _require_student(request)
    sessions = await chat_sessions.list_sessions(student_id)
    return ChatSessionList(
        sessions=[ChatSessionView(**s) for s in sessions],
        max_sessions=chat_sessions.MAX_ACTIVE_SESSIONS_PER_USER,
    )


@router.post(
    "/sessions",
    response_model=ChatSessionView,
    status_code=201,
)
async def create_chat_session(body: CreateSessionBody, request: Request):
    """Open a new chat session — capped at five per student."""
    student_id = _require_student(request)
    try:
        session = await chat_sessions.create_session(
            user_id=student_id, title=body.title
        )
    except chat_sessions.SessionLimitExceeded as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return ChatSessionView(**session)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_chat_session(session_id: str, request: Request):
    """Archive a session and purge its turns / tool audits."""
    student_id = _require_student(request)
    ok = await chat_sessions.delete_session(student_id, session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return None


@router.get(
    "/sessions/{session_id}/messages",
    response_model=ChatSessionMessageList,
)
async def get_chat_session_messages(session_id: str, request: Request):
    """Replay every turn for a session in chronological order."""
    student_id = _require_student(request)
    session = await chat_sessions.get_session(student_id, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    rows = await chat_sessions.list_messages(student_id, session_id)
    return ChatSessionMessageList(
        session_id=session_id,
        messages=[ChatSessionMessage(**r) for r in rows],
    )


# --------------------------------------------------------------------------- #
# Main chat endpoint
# --------------------------------------------------------------------------- #


@router.post("/learning-path", response_model=ChatResponseBody)
async def post_learning_path_chat(body: ChatRequestBody, request: Request):
    """Run one agentic turn over the student's analytics chat."""
    student_id = _require_student(request)

    # Resolve session — must exist and belong to this student. We allow
    # the FE to call ``POST /sessions`` first; we don't auto-create on
    # the chat endpoint, because that would silently bypass the cap.
    session = await chat_sessions.get_session(student_id, body.session_id)
    if not session:
        raise HTTPException(
            status_code=404,
            detail=(
                "Chat session not found. Open a new conversation from "
                "the chat menu before sending a message."
            ),
        )

    primary = get_provider()
    fallback = get_fallback_provider()
    tools = list_tool_definitions()

    history: list[ChatMessage] = [
        ChatMessage(role=m.role, content=m.content) for m in body.messages  # type: ignore[arg-type]
    ]

    # The latest user turn is at the end of the history. We persist it
    # *before* the LLM runs so a crash mid-call doesn't lose user input.
    latest_user_msg = next(
        (m for m in reversed(body.messages) if m.role == "user"), None
    )
    if latest_user_msg is not None:
        await chat_sessions.append_user_turn(
            user_id=student_id,
            session_id=body.session_id,
            content=latest_user_msg.content,
        )
        # First user message in a session gets promoted into the title.
        if int(session.get("message_count", 0)) == 0:
            await chat_sessions.touch_session(
                user_id=student_id,
                session_id=body.session_id,
                new_title=latest_user_msg.content,
                increment_messages=1,
            )
        else:
            await chat_sessions.touch_session(
                user_id=student_id,
                session_id=body.session_id,
                increment_messages=1,
            )

    tool_results: list[ChatToolCallResult] = []
    final_text: str = ""
    used_provider: ChatProvider = primary
    last_assistant_turn_id: str | None = None
    extra_body = {"session_id": body.session_id}

    MAX_ROUNDS = 3
    MAX_TOOL_CALLS = 6
    rounds = 0
    while rounds < MAX_ROUNDS:
        rounds += 1
        resp, used_provider = await _generate_with_fallback(
            primary=primary,
            fallback=fallback,
            messages=history,
            tools=tools,
            extra_body=extra_body,
        )

        if resp.content:
            # Always carry the latest narration forward; subsequent
            # rounds may overwrite it with a follow-up summary.
            final_text = resp.content.strip()

        # Persist the assistant turn now so audits in
        # ``ai_action_events`` can reference its ``turn_id``.
        last_assistant_turn_id = await chat_sessions.append_assistant_turn(
            user_id=student_id,
            session_id=body.session_id,
            content=resp.content or "",
            tool_calls=[
                {
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                }
                for tc in resp.tool_calls
            ],
            provider=used_provider.name,
        )
        await chat_sessions.touch_session(
            user_id=student_id,
            session_id=body.session_id,
            increment_messages=1,
        )

        if not resp.tool_calls:
            # Pure prose response — the model has nothing more to do.
            break

        history.append(
            ChatMessage(
                role="assistant",
                content=resp.content or "",
                tool_calls=list(resp.tool_calls),
            )
        )
        # Execute tools sequentially; parallel execution would race FE
        # state (e.g. two reorders on the same local path).
        for idx, call in enumerate(resp.tool_calls):
            if len(tool_results) >= MAX_TOOL_CALLS:
                logger.warning(
                    "Tool budget exhausted (%d calls); ignoring extras.",
                    MAX_TOOL_CALLS,
                )
                break
            result = await dispatch_tool(
                call.name, student_id=student_id, arguments=call.arguments,
            )
            tool_results.append(
                ChatToolCallResult(
                    name=call.name, arguments=call.arguments, result=result,
                )
            )
            history.append(
                ChatMessage(
                    role="tool",
                    name=call.name,
                    tool_call_id=call.id,
                    content=json.dumps(result, default=str),
                )
            )
            if last_assistant_turn_id:
                await chat_sessions.append_tool_audit(
                    user_id=student_id,
                    session_id=body.session_id,
                    turn_id=last_assistant_turn_id,
                    index=idx,
                    tool_name=call.name,
                    arguments=call.arguments,
                    result=result,
                )
        if len(tool_results) >= MAX_TOOL_CALLS:
            break

    if not final_text:
        # No prose came back from the model. Prefer the tool's own
        # human-readable message (every successful tool returns one)
        # over a generic "Done." synth — that produces much friendlier
        # replies and avoids spending an extra LLM round just to ask
        # for prose.
        if tool_results:
            messages: list[str] = []
            for t in tool_results:
                msg = t.result.get("message")
                if isinstance(msg, str) and msg.strip():
                    messages.append(msg.strip())
            if messages:
                final_text = " ".join(messages)
            else:
                actions = ", ".join(
                    str(t.result.get("action", t.name)) for t in tool_results
                )
                final_text = f"Done. Actions performed: {actions}."
        else:
            final_text = (
                "I'm not sure how to help with that yet. Try asking me to "
                "reorder your path, swap a slot, or open a specific tab."
            )

    return ChatResponseBody(
        reply=final_text,
        provider=used_provider.name,
        session_id=body.session_id,
        tool_results=tool_results,
    )


# --------------------------------------------------------------------------- #
# LLM provider helper (with fallback chain)
# --------------------------------------------------------------------------- #


async def _generate_with_fallback(
    *,
    primary: ChatProvider,
    fallback: ChatProvider | None,
    messages: list[ChatMessage],
    tools: list[Any],
    extra_body: dict[str, Any] | None = None,
) -> tuple[ChatResponse, ChatProvider]:
    """Run one LLM request with optional fallback on transient failure.

    Transient = ``HTTP 429`` (rate-limited) or ``HTTP 5xx``. Anything
    else propagates immediately so we don't paper over schema bugs.
    """

    async def _call(p: ChatProvider) -> ChatResponse:
        return await p.generate(
            messages=messages,
            system_prompt=_SYSTEM_PROMPT,
            tools=tools,
            extra_body=extra_body,
        )

    def _is_transient(exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            code = exc.response.status_code
            return code == 429 or 500 <= code < 600
        return isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout))

    try:
        return await _call(primary), primary
    except Exception as exc:
        if not _is_transient(exc) or fallback is None:
            _raise_provider_error(exc, primary.name)
        logger.warning(
            "Primary provider %s failed (%s); trying fallback %s.",
            primary.name,
            type(exc).__name__,
            fallback.name,
        )

    try:
        return await _call(fallback), fallback
    except Exception as exc:
        _raise_provider_error(exc, fallback.name)


def _raise_provider_error(exc: Exception, provider_name: str) -> None:
    """Convert a provider exception into a clean ``HTTPException`` for
    the FE. Centralised so primary + fallback paths look identical."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        if exc.response.status_code == 429:
            logger.warning(
                "Provider %s rate limited: %s",
                provider_name,
                exc.response.text[:200],
            )
            raise HTTPException(
                status_code=429,
                detail=(
                    "AI assistant is rate-limited right now. Please wait "
                    "about a minute and try again."
                ),
            )
        logger.warning(
            "Provider %s HTTP %s: %s",
            provider_name,
            exc.response.status_code,
            exc.response.text[:200],
        )
    else:
        logger.warning("Provider %s failed: %s", provider_name, exc)
    raise HTTPException(
        status_code=502, detail="LLM provider unavailable. Try again."
    )
