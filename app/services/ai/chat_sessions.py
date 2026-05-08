"""Persistence layer for the agentic learning-path chatbot.

Three MongoDB collections back the chat experience. Each one earns its
keep — none of these are "bootstrap-only" placeholders any more.

* ``learnez_ai.agent_runs`` — *session metadata*. One document per
  conversation a student opens. Stores the title (auto-generated from
  the first user prompt), creation timestamps and a running message
  counter so the FE can render a session picker without paging through
  every turn.
* ``elearning_raw.chat_events`` — *append-only turn log*. One document
  per chat message (user / assistant). Assistant turns also embed any
  ``tool_calls`` the model emitted so a session can be replayed later
  without re-running the LLM.
* ``elearning_raw.ai_action_events`` — *tool-call audit*. One document
  per tool the agent actually executed, with the resolved arguments
  and the JSON-serialisable result. This is the auditable trail of
  what the chatbot *did* on behalf of the student.

Why this split — instead of a single mega-collection — mirrors the
design doc (`MONGODB_AI_DATA_DESIGN.md`):

* event collections sit in the raw DB so they share retention rules
  with `activity_events` / `assessment_events`;
* the session/decision row sits in the AI DB next to risk_scores etc.

The cap of **five active sessions per student** is enforced here, in
:func:`enforce_session_cap`, so the API endpoints can stay thin.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_mongo_ai_db, get_mongo_raw_db

logger = logging.getLogger(__name__)

# Hard cap, exposed for the API layer to surface in 4xx responses.
MAX_ACTIVE_SESSIONS_PER_USER = 5

# How many characters of the first user message become the auto-title.
_AUTO_TITLE_MAX_CHARS = 60


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ai_db() -> AsyncIOMotorDatabase:
    return get_mongo_ai_db()


def _raw_db() -> AsyncIOMotorDatabase:
    return get_mongo_raw_db()


def auto_title(seed: str | None) -> str:
    """Best-effort title from the first user message.

    Trimming + capping rather than asking the LLM keeps session
    creation free of a network round-trip and works even when the LLM
    is rate-limited.
    """
    text = (seed or "").strip().replace("\n", " ")
    if not text:
        return "New conversation"
    if len(text) <= _AUTO_TITLE_MAX_CHARS:
        return text
    return text[: _AUTO_TITLE_MAX_CHARS - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Session CRUD (collection: learnez_ai.agent_runs)
# --------------------------------------------------------------------------- #


async def list_sessions(user_id: str) -> list[dict[str, Any]]:
    """Return all active sessions for ``user_id``, newest first."""
    cursor = (
        _ai_db()["agent_runs"]
        .find({"user_id": user_id, "status": "active"})
        .sort([("updated_at", -1)])
    )
    docs: list[dict[str, Any]] = []
    async for doc in cursor:
        docs.append(_serialise_session(doc))
    return docs


async def get_session(
    user_id: str, session_id: str
) -> dict[str, Any] | None:
    doc = await _ai_db()["agent_runs"].find_one(
        {"user_id": user_id, "run_id": session_id, "status": "active"}
    )
    if not doc:
        return None
    return _serialise_session(doc)


async def count_active_sessions(user_id: str) -> int:
    return await _ai_db()["agent_runs"].count_documents(
        {"user_id": user_id, "status": "active"}
    )


async def enforce_session_cap(user_id: str) -> None:
    """Raise :class:`SessionLimitExceeded` when the student is at cap."""
    n = await count_active_sessions(user_id)
    if n >= MAX_ACTIVE_SESSIONS_PER_USER:
        raise SessionLimitExceeded(
            f"You already have {n} chats. Delete one before starting a new "
            f"conversation (max {MAX_ACTIVE_SESSIONS_PER_USER})."
        )


async def create_session(
    *,
    user_id: str,
    title: str | None = None,
) -> dict[str, Any]:
    """Create a new chat session, enforcing the per-user cap."""
    await enforce_session_cap(user_id)
    now = _utc_now()
    session_id = uuid.uuid4().hex
    doc = {
        "run_id": session_id,
        "user_id": user_id,
        "actor": "student",
        "status": "active",
        "title": auto_title(title) if title else "New conversation",
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
        "schema_version": 1,
    }
    await _ai_db()["agent_runs"].insert_one(doc)
    return _serialise_session(doc)


async def delete_session(user_id: str, session_id: str) -> bool:
    """Soft-archive the session and purge its turn / audit history.

    Soft-archive keeps the metadata document around so audit reports
    and analytics joins (e.g. a future "tool calls per session" panel)
    can still resolve the run id. The chat / tool documents themselves
    are removed because they would otherwise grow unbounded.
    """
    res = await _ai_db()["agent_runs"].update_one(
        {"user_id": user_id, "run_id": session_id, "status": "active"},
        {"$set": {"status": "archived", "updated_at": _utc_now()}},
    )
    if res.modified_count == 0:
        return False
    await _raw_db()["chat_events"].delete_many(
        {"user_id": user_id, "conversation_id": session_id}
    )
    await _raw_db()["ai_action_events"].delete_many(
        {"user_id": user_id, "run_id": session_id}
    )
    return True


async def touch_session(
    *,
    user_id: str,
    session_id: str,
    new_title: str | None = None,
    increment_messages: int = 0,
) -> None:
    """Refresh ``updated_at`` and counters after a turn lands."""
    update: dict[str, Any] = {"$set": {"updated_at": _utc_now()}}
    if new_title:
        update["$set"]["title"] = auto_title(new_title)
    if increment_messages:
        update["$inc"] = {"message_count": int(increment_messages)}
    await _ai_db()["agent_runs"].update_one(
        {"user_id": user_id, "run_id": session_id, "status": "active"},
        update,
    )


def _serialise_session(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop Mongo internals + ISO-format datetimes for the API layer."""
    return {
        "session_id": str(doc.get("run_id") or ""),
        "title": str(doc.get("title") or "New conversation"),
        "status": str(doc.get("status") or "active"),
        "message_count": int(doc.get("message_count") or 0),
        "created_at": _iso(doc.get("created_at")),
        "updated_at": _iso(doc.get("updated_at")),
    }


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, str):
        return value
    return None


# --------------------------------------------------------------------------- #
# Turn log (collection: elearning_raw.chat_events)
# --------------------------------------------------------------------------- #


async def append_user_turn(
    *,
    user_id: str,
    session_id: str,
    content: str,
) -> str:
    """Persist a user message and return its turn id."""
    turn_id = uuid.uuid4().hex
    now = _utc_now()
    await _raw_db()["chat_events"].insert_one(
        {
            "conversation_id": session_id,
            "turn_id": turn_id,
            "user_id": user_id,
            "role": "user",
            "content": content,
            "tool_calls": [],
            "event_time": now,
            "schema_version": 1,
        }
    )
    return turn_id


async def append_assistant_turn(
    *,
    user_id: str,
    session_id: str,
    content: str,
    tool_calls: list[dict[str, Any]] | None = None,
    provider: str | None = None,
) -> str:
    turn_id = uuid.uuid4().hex
    now = _utc_now()
    await _raw_db()["chat_events"].insert_one(
        {
            "conversation_id": session_id,
            "turn_id": turn_id,
            "user_id": user_id,
            "role": "assistant",
            "content": content,
            "tool_calls": list(tool_calls or []),
            "provider": provider,
            "event_time": now,
            "schema_version": 1,
        }
    )
    return turn_id


async def list_messages(user_id: str, session_id: str) -> list[dict[str, Any]]:
    """Replay messages for the FE in chronological order."""
    cursor = (
        _raw_db()["chat_events"]
        .find({"user_id": user_id, "conversation_id": session_id})
        .sort([("event_time", 1)])
    )
    out: list[dict[str, Any]] = []
    async for doc in cursor:
        out.append(
            {
                "turn_id": str(doc.get("turn_id") or ""),
                "role": str(doc.get("role") or ""),
                "content": str(doc.get("content") or ""),
                "tool_calls": list(doc.get("tool_calls") or []),
                "event_time": _iso(doc.get("event_time")),
                "provider": doc.get("provider"),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Tool-call audit (collection: elearning_raw.ai_action_events)
# --------------------------------------------------------------------------- #


async def append_tool_audit(
    *,
    user_id: str,
    session_id: str,
    turn_id: str,
    index: int,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """One row per executed tool call.

    ``idempotency_key`` is unique per (turn, index) so retries don't
    duplicate audits if the agent loop is replayed.
    """
    idempotency_key = f"tool:{session_id}:{turn_id}:{index}"
    await _raw_db()["ai_action_events"].update_one(
        {"idempotency_key": idempotency_key},
        {
            "$set": {
                "run_id": session_id,
                "conversation_id": session_id,
                "turn_id": turn_id,
                "user_id": user_id,
                "actor": "assistant",
                "action_name": tool_name,
                "tool_name": tool_name,
                "input": arguments,
                "result": result,
                "event_time": _utc_now(),
                "idempotency_key": idempotency_key,
                "schema_version": 1,
            }
        },
        upsert=True,
    )


# --------------------------------------------------------------------------- #
# Index helpers — called once on app start so the chat path doesn't
# silently rely on the bootstrap script having been run earlier.
# --------------------------------------------------------------------------- #


async def ensure_chat_indexes() -> None:
    """Create chat-related indexes if they don't already exist."""
    ai = _ai_db()
    raw = _raw_db()

    # agent_runs already has run_id + user_id indexes from bootstrap;
    # add a (user_id, status, updated_at) compound to make the picker
    # query (active sessions newest-first) a single index hit.
    await ai["agent_runs"].create_index(
        [("user_id", 1), ("status", 1), ("updated_at", -1)],
        name="user_status_updated_idx",
    )
    # Compound covers list-messages and delete-by-session paths.
    await raw["chat_events"].create_index(
        [("user_id", 1), ("conversation_id", 1), ("event_time", 1)],
        name="user_conversation_time_idx",
    )
    # Audit lookups by (user, session) are the hot path; the existing
    # idempotency_key unique index handles dedupe on the write side.
    await raw["ai_action_events"].create_index(
        [("user_id", 1), ("run_id", 1), ("event_time", 1)],
        name="user_run_time_idx",
    )


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class SessionLimitExceeded(Exception):
    """Raised when a student tries to exceed the per-user cap."""


__all__ = [
    "MAX_ACTIVE_SESSIONS_PER_USER",
    "SessionLimitExceeded",
    "append_assistant_turn",
    "append_tool_audit",
    "append_user_turn",
    "auto_title",
    "count_active_sessions",
    "create_session",
    "delete_session",
    "enforce_session_cap",
    "ensure_chat_indexes",
    "get_session",
    "list_messages",
    "list_sessions",
    "touch_session",
]
