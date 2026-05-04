"""Activity Tracking - Login, viewing duration, submissions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import DbDep

router = APIRouter(prefix="/activity", tags=["Activity Tracking"])

_ALLOWED_EVENT_TYPES = frozenset(
    {
        "login",
        "logout",
        "page_view",
        "material_open",
        "material_close",
        "session_heartbeat",
        "submission_created",
        "submission_updated",
        "graded",
        "graded_finalized",
    }
)


class ActivityLogBody(BaseModel):
    user_id: str
    event_type: str
    event_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    course_id: int | None = None
    module_id: int | None = None
    material_id: int | None = None
    submission_id: int | None = None
    duration_sec: int | None = Field(default=None, ge=0)
    properties: dict[str, Any] = Field(default_factory=dict)
    source: Literal["web", "api", "job", "agent"] = "api"


@router.post("/log")
async def log_activity(db: DbDep, body: ActivityLogBody):
    """Log user activity into Mongo raw `activity_events`."""
    event_type = (body.event_type or "").strip()
    if event_type not in _ALLOWED_EVENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported event_type={event_type}. Allowed: {sorted(_ALLOWED_EVENT_TYPES)}",
        )

    event_id = f"activity-{uuid4().hex}"
    idempotency_key = f"{body.user_id}:{event_type}:{int(body.event_time.timestamp())}:{body.course_id or 0}:{body.module_id or 0}"
    doc = {
        "event_id": event_id,
        "event_type": event_type,
        "event_time": body.event_time,
        "created_at": datetime.now(timezone.utc),
        "source": body.source,
        "schema_version": 1,
        "user_id": body.user_id,
        "course_id": body.course_id,
        "module_id": body.module_id,
        "material_id": body.material_id,
        "submission_id": body.submission_id,
        "duration_sec": body.duration_sec,
        "properties": body.properties,
        "idempotency_key": idempotency_key,
    }

    await db["activity_events"].update_one(
        {"idempotency_key": idempotency_key},
        {"$set": doc},
        upsert=True,
    )
    return {
        "status": "ok",
        "collection": "activity_events",
        "event_id": event_id,
        "idempotency_key": idempotency_key,
    }


@router.get("/{user_id}")
async def get_user_activity(
    db: DbDep,
    user_id: str,
    since_days: int = Query(default=30, ge=1, le=120),
):
    """Get recent activity summary and latest events for AI analysis/debugging."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=since_days)
    q = {"user_id": user_id, "event_time": {"$gte": start, "$lt": end}}
    events = await db["activity_events"].find(q).sort([("event_time", -1)]).to_list(length=200)
    if not events:
        raise HTTPException(status_code=404, detail=f"No activity events found for user_id={user_id}")

    by_type: dict[str, int] = {}
    total_duration_sec = 0
    for e in events:
        et = str(e.get("event_type") or "unknown")
        by_type[et] = by_type.get(et, 0) + 1
        total_duration_sec += int(e.get("duration_sec") or 0)

    return {
        "user_id": user_id,
        "since_days": since_days,
        "event_count": len(events),
        "event_counts_by_type": by_type,
        "total_duration_sec": total_duration_sec,
        "latest_events": [
            {
                "event_type": e.get("event_type"),
                "event_time": e.get("event_time"),
                "course_id": e.get("course_id"),
                "module_id": e.get("module_id"),
                "duration_sec": e.get("duration_sec"),
            }
            for e in events[:20]
        ],
    }
