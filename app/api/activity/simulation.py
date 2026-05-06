"""Simulation / ML ingestion: batch-write raw events to Mongo with clock-warp support.

Protected by ``X-Simulation-Secret`` (``ML_SIMULATION_SECRET`` env). Not for production
unless the secret is strong and known only to operators.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from pymongo import ReplaceOne

from app.core.database import get_mongo_raw_db

router = APIRouter(prefix="/sim", tags=["Activity & AI"])

_ALLOWED_COLLECTIONS = frozenset(
    {
        "activity_events",
        "assessment_events",
        "attendance_events",
        "content_events",
        "simulation_users",
    }
)


def _require_sim_secret(provided: str | None) -> None:
    expected = os.getenv("ML_SIMULATION_SECRET", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="ML_SIMULATION_SECRET is not configured; simulation ingest is disabled.",
        )
    if not provided or provided.strip() != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Simulation-Secret header.")


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _normalize_event_document(
    doc: dict[str, Any],
    *,
    header_event_time: datetime | None,
    clock_warp: bool,
) -> dict[str, Any]:
    out = dict(doc)
    et = _parse_dt(out.get("event_time")) or header_event_time
    if et is None:
        et = datetime.now(timezone.utc)
    out["event_time"] = et
    if clock_warp:
        out["created_at"] = et
    else:
        ca = _parse_dt(out.get("created_at"))
        out["created_at"] = ca if ca is not None else datetime.now(timezone.utc)
    return out


def _normalize_reference_user(doc: dict[str, Any], *, clock_warp: bool) -> dict[str, Any]:
    """Reference rows for ML joins — no ``event_time``."""
    out = dict(doc)
    now = datetime.now(timezone.utc)
    ca = _parse_dt(out.get("created_at"))
    out["created_at"] = ca if ca is not None else now
    if clock_warp and out.get("simulated_from"):
        st = _parse_dt(out.get("simulated_from"))
        if st:
            out["created_at"] = st
    return out


class IngestBatchBody(BaseModel):
    collection: str
    documents: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/ingest-batch", summary="Batch upsert simulation events (Mongo raw DB)")
async def ingest_batch(
    body: IngestBatchBody,
    x_simulation_secret: str | None = Header(default=None, alias="X-Simulation-Secret"),
    x_event_time: str | None = Header(default=None, alias="X-Event-Time"),
    x_clock_warp: str | None = Header(default=None, alias="X-Clock-Warp"),
) -> dict[str, int]:
    """Upsert event documents by ``idempotency_key``.

    - Each document should include ``event_time`` (ISO). If omitted, ``X-Event-Time`` is used
      (same instant for every document in the batch — prefer per-doc ``event_time``).
    - When ``X-Clock-Warp: 1`` (or ``true``), ``created_at`` is set equal to ``event_time`` so
      backfilled timelines look consistent for training pipelines.
    """
    _require_sim_secret(x_simulation_secret)
    name = (body.collection or "").strip()
    if name not in _ALLOWED_COLLECTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"collection must be one of: {sorted(_ALLOWED_COLLECTIONS)}",
        )
    if not body.documents:
        return {"upserted": 0}

    header_et = _parse_dt(x_event_time) if x_event_time else None
    warp = (x_clock_warp or "").strip().lower() in {"1", "true", "yes"}

    db = get_mongo_raw_db()
    col = db[name]

    ops: list[ReplaceOne[Any]] = []
    for raw in body.documents:
        key = raw.get("idempotency_key")
        if not key:
            raise HTTPException(status_code=400, detail="Every document must include idempotency_key")
        if name == "simulation_users":
            doc = _normalize_reference_user(raw, clock_warp=warp)
        else:
            doc = _normalize_event_document(raw, header_event_time=header_et, clock_warp=warp)
        ops.append(ReplaceOne({"idempotency_key": key}, doc, upsert=True))

    await col.bulk_write(ops, ordered=False)
    return {"written": len(ops)}
