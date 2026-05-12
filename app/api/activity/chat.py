from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.deps import AiDbDep, RawDbDep
from app.core.dependencies import ROLE_MAP
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

MAX_ACTIVE_SESSIONS_PER_USER = 5
MAX_REPLAY_MESSAGES = 200


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


class ChatRequestMessage(BaseModel):
    role: str = Field(pattern=r"^(user|assistant)$")
    content: str = Field(min_length=1, max_length=4000)


class ChatRequestBody(BaseModel):
    # ``session_id`` is required for every turn — the picker creates a
    # session lazily on first message and reuses it for the rest of the
    # conversation. The backend refuses unknown ids so a stale FE
    # cannot accidentally write into someone else's chat.
    session_id: str = Field(min_length=1, max_length=64)
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


# --------------------------------------------------------------------------- #
# Session-management contracts
# --------------------------------------------------------------------------- #


class ChatSessionDTO(BaseModel):
    """One row from ``agent_runs`` shaped for the FE picker."""

    session_id: str
    title: str
    status: str
    message_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ChatSessionListResponse(BaseModel):
    sessions: list[ChatSessionDTO]
    max_sessions: int = MAX_ACTIVE_SESSIONS_PER_USER


class ChatSessionCreateBody(BaseModel):
    title: str | None = Field(default=None, max_length=120)


class ChatSessionMessageDTO(BaseModel):
    """One row from ``chat_events`` exposed to the FE.

    Tool-call results are *not* re-emitted on replay (only the prose
    turns) — they're written separately into ``ai_action_events`` and
    their effects on local state were applied at the time of execution.
    Replaying them on history load would be confusing.
    """

    turn_id: str
    role: str
    content: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    event_time: datetime | None = None
    provider: str | None = None


class ChatSessionMessageListResponse(BaseModel):
    session_id: str
    messages: list[ChatSessionMessageDTO]


# --------------------------------------------------------------------------- #
# Persistence helpers
# --------------------------------------------------------------------------- #


def _require_student(request: Request) -> str:
    """Resolve the authenticated student id, or raise.

    Centralised so every session endpoint applies the same scope check
    — chat is a *student-only* surface, never exposed to lecturer or
    admin accounts (their UI never opens this widget).
    """
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


def _new_session_id() -> str:
    """UUID4 hex — short enough to fit a URL path, plenty unique."""
    return uuid.uuid4().hex


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _idempotency_key(*parts: Any) -> str:
    """Stable hash used as the unique key on ai_action_events.

    Built from the run + tool name + arguments hash + timestamp so two
    identical tool calls in the same turn collapse into one row.
    """
    return hashlib.sha1("::".join(str(p) for p in parts).encode("utf-8")).hexdigest()


async def _load_session(ai_db: AiDbDep, *, session_id: str, student_id: str) -> dict[str, Any]:
    """Resolve a session document, validating ownership.

    Soft-deleted rows (``status == "deleted"``) are treated as gone —
    we don't surface them on read and we forbid writing new turns to
    them. The 404 / 410 split lets the FE distinguish a stale id from
    an explicit deletion.
    """
    doc = await ai_db["agent_runs"].find_one({"run_id": session_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Chat session not found")
    if doc.get("user_id") != student_id:
        # Don't leak whether the session exists; behave as if missing.
        raise HTTPException(status_code=404, detail="Chat session not found")
    if doc.get("status") == "deleted":
        raise HTTPException(status_code=410, detail="Chat session was deleted")
    return doc


def _session_doc_to_dto(doc: dict[str, Any]) -> ChatSessionDTO:
    """Project a raw Mongo doc onto the public-facing DTO."""
    return ChatSessionDTO(
        session_id=str(doc.get("run_id")),
        title=str(doc.get("title") or "Untitled chat"),
        status=str(doc.get("status") or "active"),
        message_count=int(doc.get("message_count") or 0),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


async def _append_chat_event(
    raw_db: RawDbDep,
    *,
    session_id: str,
    student_id: str,
    turn_id: str,
    role: str,
    content: str,
    provider: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> None:
    """Insert one row into ``elearning_raw.chat_events``.

    Keyed by ``(conversation_id, turn_id)`` — the same compound unique
    index used by ``mongodb_bootstrap``. We rely on the unique
    constraint to make double-writes a no-op rather than a duplicate
    row.
    """
    doc = {
        "conversation_id": session_id,
        "turn_id": turn_id,
        "user_id": student_id,
        "role": role,
        "content": content,
        "tool_calls": tool_calls or [],
        "event_time": _now_utc(),
        "source": "web",
        "schema_version": 1,
        "provider": provider,
    }
    try:
        await raw_db["chat_events"].insert_one(doc)
    except Exception as exc:  # noqa: BLE001 — duplicate-key etc. are best-effort
        logger.debug("chat_events insert skipped: %s", exc)


async def _append_action_event(
    raw_db: RawDbDep,
    *,
    session_id: str,
    student_id: str,
    action_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Insert one tool-execution row into ``ai_action_events``.

    Best-effort: a duplicate idempotency_key (rare — two identical
    calls in the same millisecond) silently no-ops.
    """
    args_blob = json.dumps(arguments or {}, default=str, sort_keys=True)
    idem = _idempotency_key(session_id, action_name, args_blob, _now_utc().isoformat())
    doc = {
        "idempotency_key": idem,
        "run_id": session_id,
        "user_id": student_id,
        "action_name": action_name,
        "arguments": arguments or {},
        "result": result or {},
        "event_time": _now_utc(),
        "schema_version": 1,
    }
    try:
        await raw_db["ai_action_events"].insert_one(doc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ai_action_events insert skipped: %s", exc)


async def _bump_session_after_turn(
    ai_db: AiDbDep, *, session_id: str, added_messages: int, derived_title: str | None
) -> None:
    """Bump ``message_count`` + ``updated_at`` (and optionally set the
    title from the user's first message). Centralised so we keep
    agent_runs in lock-step with chat_events without scattering
    update fragments across the agent loop.
    """
    await ai_db["agent_runs"].update_one(
        {"run_id": session_id},
        {
            "$inc": {"message_count": added_messages},
            "$set": {"updated_at": _now_utc()},
        },
    )
    if derived_title:
        # Second write: only set the title if it's still on its
        # default sentinel. We do this via a separate query (rather
        # than ``$cond`` inside the first update) so it works on
        # MongoDB <5.0 too, without aggregation-pipeline updates.
        await ai_db["agent_runs"].update_one(
            {
                "run_id": session_id,
                "$or": [{"title": ""}, {"title": None}, {"title": "New chat"}],
            },
            {"$set": {"title": derived_title}},
        )


# --------------------------------------------------------------------------- #
# Session management endpoints
# --------------------------------------------------------------------------- #


@router.get("/sessions", response_model=ChatSessionListResponse)
async def list_chat_sessions(
    ai_db: AiDbDep,
    request: Request,
):
    """List active chat sessions for the current student.

    Ordering: most recently active first (``updated_at`` desc, then
    ``created_at`` desc as a tiebreaker for never-replied sessions).
    Soft-deleted rows are filtered out — they live on in Mongo for
    audit but the picker never shows them.
    """
    student_id = _require_student(request)
    cursor = (
        ai_db["agent_runs"]
        .find({"user_id": student_id, "status": {"$ne": "deleted"}})
        .sort([("updated_at", -1), ("created_at", -1)])
        .limit(MAX_ACTIVE_SESSIONS_PER_USER * 2)
    )
    docs = await cursor.to_list(length=MAX_ACTIVE_SESSIONS_PER_USER * 2)
    return ChatSessionListResponse(
        sessions=[_session_doc_to_dto(d) for d in docs],
        max_sessions=MAX_ACTIVE_SESSIONS_PER_USER,
    )


@router.post("/sessions", response_model=ChatSessionDTO, status_code=status.HTTP_201_CREATED)
async def create_chat_session(
    body: ChatSessionCreateBody,
    ai_db: AiDbDep,
    request: Request,
):
    """Spin up a new session row.

    Returns ``409 Conflict`` when the student already has
    ``MAX_ACTIVE_SESSIONS_PER_USER`` non-deleted rows — the picker
    surfaces this as a "Delete one to start a new chat" error.
    """
    student_id = _require_student(request)
    active_count = await ai_db["agent_runs"].count_documents(
        {"user_id": student_id, "status": {"$ne": "deleted"}}
    )
    if active_count >= MAX_ACTIVE_SESSIONS_PER_USER:
        raise HTTPException(
            status_code=409,
            detail=(
                f"You already have {MAX_ACTIVE_SESSIONS_PER_USER} active chats. "
                "Delete one before starting a new conversation."
            ),
        )

    title = (body.title or "").strip() or "New chat"
    now = _now_utc()
    doc = {
        "run_id": _new_session_id(),
        "user_id": student_id,
        "title": title[:120],
        "status": "active",
        "message_count": 0,
        "created_at": now,
        "updated_at": now,
        "schema_version": 1,
    }
    await ai_db["agent_runs"].insert_one(doc)
    return _session_doc_to_dto(doc)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat_session(
    session_id: str,
    ai_db: AiDbDep,
    raw_db: RawDbDep,
    request: Request,
):
    """Soft-delete a chat session and wipe its turn log.

    Why soft-delete on ``agent_runs`` but hard-delete on
    ``chat_events`` / ``ai_action_events``? The session metadata is
    tiny and useful for analytics ("how many chats per student");
    the turn content can be very large and is the part the student
    actually wants gone. This split also keeps the unique
    ``(conversation_id, turn_id)`` index from blocking a future
    re-create with the same id (we never reuse ids — UUID4).
    """
    student_id = _require_student(request)
    doc = await ai_db["agent_runs"].find_one({"run_id": session_id})
    if not doc or doc.get("user_id") != student_id:
        raise HTTPException(status_code=404, detail="Chat session not found")

    now = _now_utc()
    await ai_db["agent_runs"].update_one(
        {"run_id": session_id},
        {"$set": {"status": "deleted", "deleted_at": now, "updated_at": now}},
    )
    try:
        await raw_db["chat_events"].delete_many({"conversation_id": session_id})
        await raw_db["ai_action_events"].delete_many({"run_id": session_id})
    except Exception:  # noqa: BLE001 — content deletion is best-effort
        logger.exception("Failed to wipe chat content for session %s", session_id)


@router.get("/sessions/{session_id}/messages", response_model=ChatSessionMessageListResponse)
async def get_chat_session_messages(
    session_id: str,
    ai_db: AiDbDep,
    raw_db: RawDbDep,
    request: Request,
):
    """Return the chronological turn log for one session.

    Used by the FE when the user switches between saved chats — the
    picker calls this once and then renders the messages locally. We
    cap the result to ``MAX_REPLAY_MESSAGES`` so a runaway session
    can't blow up the client.
    """
    student_id = _require_student(request)
    await _load_session(ai_db, session_id=session_id, student_id=student_id)
    cursor = (
        raw_db["chat_events"]
        .find({"conversation_id": session_id, "user_id": student_id})
        .sort([("event_time", 1)])
        .limit(MAX_REPLAY_MESSAGES)
    )
    docs = await cursor.to_list(length=MAX_REPLAY_MESSAGES)
    return ChatSessionMessageListResponse(
        session_id=session_id,
        messages=[
            ChatSessionMessageDTO(
                turn_id=str(d.get("turn_id") or ""),
                role=str(d.get("role") or "assistant"),
                content=str(d.get("content") or ""),
                tool_calls=list(d.get("tool_calls") or []),
                event_time=d.get("event_time"),
                provider=d.get("provider"),
            )
            for d in docs
        ],
    )


# --------------------------------------------------------------------------- #
# Suggestion endpoint — dynamic quick-prompt chips
# --------------------------------------------------------------------------- #


class ChatSuggestionDTO(BaseModel):
    """One quick-prompt chip rendered above the chat textarea."""

    label: str
    prompt: str
    kind: str  # ``coaching`` | ``navigation`` | ``starter``


class ChatSuggestionListResponse(BaseModel):
    suggestions: list[ChatSuggestionDTO]


def _avg_score_per_course(student_id: str) -> dict[int, dict[str, Any]]:
    """Compute per-course graded averages for the chat suggestion logic.

    Returns ``{course_id: {"avg_10": float, "count": int, "code": str,
    "title": str}}``. Uses the analytics helper so the numbers match
    what the analytics tabs render.
    """
    try:
        from app.api.activity.analytics import _student_submission_meta  # noqa: WPS433
        from app.core.database import get_supabase  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        logger.warning("suggestions helper import failed: %s", exc)
        return {}

    sb = get_supabase(service_role=True)
    if not sb:
        return {}

    enr = (
        sb.table("course_enrollments")
        .select("course_id")
        .eq("student_id", student_id)
        .execute()
        .data
        or []
    )
    course_ids = {int(r["course_id"]) for r in enr if r.get("course_id") is not None}
    if not course_ids:
        return {}

    course_rows = (
        sb.table("courses")
        .select("id, course_code, title")
        .in_("id", sorted(course_ids))
        .execute()
        .data
        or []
    )
    code_lookup = {int(r["id"]): r for r in course_rows}

    sub_rows, assignment_meta = _student_submission_meta(student_id, course_ids=course_ids)
    bucket: dict[int, dict[str, Any]] = {}
    for sub in sub_rows:
        if not sub.get("is_corrected") or sub.get("final_score") is None:
            continue
        aid = sub.get("assignment_id")
        if aid is None:
            continue
        meta = assignment_meta.get(int(aid))
        if not meta:
            continue
        cid = meta.get("course_id")
        total = float(meta.get("total_score") or 0.0)
        if not cid or total <= 0:
            continue
        # Rescale the per-assignment grade onto the 0..10 scale so the
        # chip can speak the same language as the GPA card.
        normalised = max(0.0, min(10.0, float(sub["final_score"]) * (10.0 / total)))
        entry = bucket.setdefault(int(cid), {"sum": 0.0, "count": 0})
        entry["sum"] += normalised
        entry["count"] += 1

    out: dict[int, dict[str, Any]] = {}
    for cid, agg in bucket.items():
        if agg["count"] <= 0:
            continue
        course = code_lookup.get(cid)
        if not course:
            continue
        out[cid] = {
            "avg_10": round(agg["sum"] / agg["count"], 2),
            "count": agg["count"],
            "code": str(course.get("course_code") or ""),
            "title": str(course.get("title") or ""),
        }
    return out


def _build_chat_suggestions(student_id: str) -> list[ChatSuggestionDTO]:
    """Personalised quick-prompts.

    Strategy:
    1. If the student has no graded submissions yet → starter prompts.
    2. Otherwise mix one *coaching* prompt about their worst course,
       one about their strongest course, and a couple of navigation
       prompts that always make sense.

    We intentionally cap the list at 5 chips so the UI row never
    wraps.
    """
    per_course = _avg_score_per_course(student_id)
    suggestions: list[ChatSuggestionDTO] = []

    if not per_course:
        # The bot has no grade history to coach off — point the
        # student at the build-path and orientation flows instead.
        return [
            ChatSuggestionDTO(
                label="Build me a starter SE path",
                prompt="I'm a new SE student. Build me a starter learning path I can iterate on.",
                kind="starter",
            ),
            ChatSuggestionDTO(
                label="Show my dropout risk",
                prompt="Open my dropout risk tab and walk me through what it means.",
                kind="navigation",
            ),
            ChatSuggestionDTO(
                label="Explain the analytics view",
                prompt="What do the four analytics tabs show, and which one should I check first?",
                kind="navigation",
            ),
        ]

    ranked = sorted(per_course.values(), key=lambda c: c["avg_10"])
    weakest = ranked[0]
    strongest = ranked[-1]
    single_course = weakest is strongest

    # Use the course code if we have one (e.g. ``IT404``), otherwise
    # fall back to the human title so the chip still makes sense.
    def _ref(course: dict[str, Any]) -> str:
        return course["code"] or course["title"]

    # Single-course path — emit exactly one course-specific coaching
    # chip so we don't double-render the same prompt. The weakest /
    # strongest branches below handle the multi-course case.
    if single_course:
        suggestions.append(
            ChatSuggestionDTO(
                label=f"How am I doing in {_ref(weakest)}?",
                prompt=(
                    f"Walk me through my progress in {_ref(weakest)} "
                    f"({weakest['avg_10']:.1f}/10 so far) and tell me what to focus on next."
                ),
                kind="coaching",
            )
        )
    else:
        if weakest["avg_10"] < 7.0:
            suggestions.append(
                ChatSuggestionDTO(
                    label=f"Why am I struggling in {_ref(weakest)}?",
                    prompt=(
                        f"Why is my grade in {_ref(weakest)} ({weakest['avg_10']:.1f}/10) "
                        "lower than my other courses? What should I focus on first?"
                    ),
                    kind="coaching",
                )
            )
        if strongest["avg_10"] >= 7.5:
            suggestions.append(
                ChatSuggestionDTO(
                    label=f"How am I doing in {_ref(strongest)}?",
                    prompt=(
                        f"Summarise how I'm doing in {_ref(strongest)} and tell me whether "
                        "I should keep my current rhythm or push harder."
                    ),
                    kind="coaching",
                )
            )

    suggestions.append(
        ChatSuggestionDTO(
            label="Summarise my recent grades",
            prompt="Summarise my recent grades across every course and flag anything I should worry about.",
            kind="coaching",
        )
    )
    suggestions.append(
        ChatSuggestionDTO(
            label="Open my behavior tab",
            prompt="Open my behavior tab — I want to see my engagement signals this week.",
            kind="navigation",
        )
    )

    # Belt-and-braces dedupe by label so the React keys are always
    # unique even if a future tweak accidentally emits the same
    # prompt twice. Preserves insertion order.
    seen: set[str] = set()
    deduped: list[ChatSuggestionDTO] = []
    for s in suggestions:
        if s.label in seen:
            continue
        seen.add(s.label)
        deduped.append(s)

    # Hard cap at 5 chips so the bar never wraps onto a second line in
    # the chat panel.
    return deduped[:5]


@router.get("/suggestions", response_model=ChatSuggestionListResponse)
async def get_chat_suggestions(request: Request):
    """Personalised quick-prompts for the chat textarea.

    Cheap to compute — a few Supabase reads aggregated in-thread.
    The FE caches the response for the lifetime of the analytics
    page; it re-fetches automatically when a graded submission
    invalidates the ``["analytics"]`` query key.
    """
    student_id = _require_student(request)
    suggestions = await asyncio.to_thread(_build_chat_suggestions, student_id)
    return ChatSuggestionListResponse(suggestions=suggestions)


# --------------------------------------------------------------------------- #
# Agent loop endpoint
# --------------------------------------------------------------------------- #


@router.post("/learning-path", response_model=ChatResponseBody)
async def post_learning_path_chat(
    body: ChatRequestBody,
    ai_db: AiDbDep,
    raw_db: RawDbDep,
    request: Request,
):
    """Run one agentic turn over the student's analytics chat.

    Persistence model
    -----------------
    Every turn writes:

    * one ``chat_events`` row for the user's new prompt;
    * one ``chat_events`` row for the assistant's final reply (with
      the full ``tool_calls`` list snapshotted alongside);
    * one ``ai_action_events`` row per executed tool, for the audit
      trail consumed by the lecturer-side dashboards.

    The ``agent_runs.message_count`` / ``updated_at`` metadata is bumped
    once at the end so a single turn shows up as +2 messages in the
    picker. None of these writes are blocking — if Mongo is briefly
    unreachable, the agent still returns its reply, we just lose the
    audit row (logged at WARN).
    """
    student_id = _require_student(request)
    session = await _load_session(
        ai_db, session_id=body.session_id, student_id=student_id
    )
    session_id = str(session.get("run_id"))

    primary = get_provider()
    fallback = get_fallback_provider()
    tools = list_tool_definitions()

    # Snapshot the user's incoming prompt before any LLM work. We log
    # it eagerly so a downstream LLM failure still leaves a trace of
    # what the student asked.
    user_message = body.messages[-1] if body.messages else None
    user_turn_id = _new_session_id()
    if user_message and user_message.role == "user":
        await _append_chat_event(
            raw_db,
            session_id=session_id,
            student_id=student_id,
            turn_id=user_turn_id,
            role="user",
            content=user_message.content,
        )

    history: list[ChatMessage] = [
        ChatMessage(role=m.role, content=m.content) for m in body.messages  # type: ignore[arg-type]
    ]

    tool_results: list[ChatToolCallResult] = []
    final_text: str = ""
    used_provider: ChatProvider = primary

    # Bounded multi-turn agent loop: each round is one LLM call followed
    # by deterministic execution of any tools the model requested. We
    # cap the loop at ``MAX_ROUNDS`` so a misbehaving model can't burn
    # quota in a runaway. Two-round loops are common (discover via
    # summarize_recent_grades → act with reorder_path / explain_*), so
    # 3 rounds is enough headroom for tool chaining without exploding
    # RPM. The OpenRouter cascade handles per-model 429s internally so
    # this loop only sees clean responses or hard failures.
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
        )

        if resp.content:
            # Always carry the latest narration forward; subsequent
            # rounds may overwrite it with a follow-up summary.
            final_text = resp.content.strip()

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
        for call in resp.tool_calls:
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

    # ---------- Persist assistant turn + audit each tool call ---------- #
    # We tag the assistant row with the full ``tool_calls`` snapshot so
    # a replay later can faithfully reconstruct *what* the agent did
    # (the FE doesn't re-apply the effects on replay, but lecturers
    # auditing the conversation may want to see them).
    assistant_turn_id = _new_session_id()
    tool_calls_blob = [
        {
            "name": t.name,
            "arguments": t.arguments,
            "status": str(t.result.get("status") or ""),
            "action": t.result.get("action"),
        }
        for t in tool_results
    ]
    try:
        await _append_chat_event(
            raw_db,
            session_id=session_id,
            student_id=student_id,
            turn_id=assistant_turn_id,
            role="assistant",
            content=final_text,
            provider=used_provider.name,
            tool_calls=tool_calls_blob,
        )
        for t in tool_results:
            await _append_action_event(
                raw_db,
                session_id=session_id,
                student_id=student_id,
                action_name=t.name,
                arguments=t.arguments,
                result=t.result,
            )
        # Pick the user's first message as the default title if the
        # session is still on its default. Truncated to 60 chars to
        # match the FE's `createChatSession(title.slice(0,60))` call.
        derived_title: str | None = None
        if session.get("message_count", 0) == 0 and user_message:
            derived_title = user_message.content[:60].strip() or None
        await _bump_session_after_turn(
            ai_db,
            session_id=session_id,
            added_messages=2,
            derived_title=derived_title,
        )
    except Exception:  # noqa: BLE001 — persistence is best-effort
        logger.exception("Failed to persist chat turn for session %s", session_id)

    return ChatResponseBody(
        reply=final_text,
        provider=used_provider.name,
        session_id=session_id,
        tool_results=tool_results,
    )


async def _generate_with_fallback(
    *,
    primary: ChatProvider,
    fallback: ChatProvider | None,
    messages: list[ChatMessage],
    tools: list[Any],
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
