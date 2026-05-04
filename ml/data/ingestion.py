"""Initial Supabase -> MongoDB ingestion scaffold for Module 4.

This module is the bridge between the LMS transactional store and the AI data
layer. It is intentionally conservative:

- read from Supabase with service-role auth
- write immutable event docs to MongoDB
- leave feature aggregation and model scoring to separate jobs

The concrete event transformation logic will be extended in the next phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import asyncio
from typing import Sequence

from pymongo.errors import AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError

from app.core.database import get_mongo_raw_db, get_supabase

from .contracts import (
    AssessmentEvent,
    AttendanceEvent,
    ActivityEvent,
    ContentEvent,
    RAW_EVENT_COLLECTION_BY_SOURCE,
    SUPABASE_SOURCE_SPECS,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class ExtractionWindow:
    since: datetime | None = None
    until: datetime | None = None


class SupabaseSourceReader:
    """Read source-of-truth LMS records through the service-role client."""

    def __init__(self) -> None:
        self._client = get_supabase(service_role=True)
        if self._client is None:
            raise RuntimeError("Supabase service-role client is not configured")

    def fetch_table(
        self,
        table_name: str,
        window: ExtractionWindow | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = self._client.table(table_name).select("*")
        if window and window.since and table_name in {"assignment_submissions", "course_attendance", "notifications"}:
            query = query.gte("created_at", window.since.isoformat())
        if window and window.since and table_name in {"module_materials", "assignments", "courses", "modules"}:
            query = query.gte("created_at", window.since.isoformat())
        if window and window.until and table_name in {"assignment_submissions", "course_attendance", "notifications"}:
            query = query.lt("created_at", window.until.isoformat())
        if limit is not None:
            query = query.limit(limit)
        result = query.execute()
        return list(result.data or [])

    def fetch_source_bundle(
        self,
        window: ExtractionWindow | None = None,
        limit: int | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        bundle: dict[str, list[dict[str, Any]]] = {}
        for spec in SUPABASE_SOURCE_SPECS:
            bundle[spec.table_name] = self.fetch_table(spec.table_name, window, limit)
        return bundle

    def fetch_sample_bundle(self, limit_per_table: int = 25) -> dict[str, list[dict[str, Any]]]:
        return self.fetch_source_bundle(limit=limit_per_table)

    @staticmethod
    def _chunk(items: list[Any], size: int = 200) -> list[list[Any]]:
        return [items[i : i + size] for i in range(0, len(items), size)]

    def _fetch_in(
        self,
        table_name: str,
        column: str,
        values: list[Any],
        *,
        select: str = "*",
    ) -> list[dict[str, Any]]:
        if not values:
            return []
        out: list[dict[str, Any]] = []
        for batch in self._chunk(values):
            res = self._client.table(table_name).select(select).in_(column, batch).execute()
            out.extend(list(res.data or []))
        return out

    def fetch_student_scoped_bundle(self, student_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Fetch source bundle focused on specific students and their related context.

        This is used when onboarding a small set of real students into the AI
        pipeline quickly (Supabase -> Mongo events -> weekly features).
        """
        student_ids = [s.strip() for s in student_ids if str(s).strip()]
        if not student_ids:
            return {spec.table_name: [] for spec in SUPABASE_SOURCE_SPECS}

        bundle: dict[str, list[dict[str, Any]]] = {spec.table_name: [] for spec in SUPABASE_SOURCE_SPECS}

        users = self._fetch_in("users", "user_id", student_ids)
        enrollments = self._fetch_in("course_enrollments", "student_id", student_ids)
        attendance = self._fetch_in("course_attendance", "student_id", student_ids)
        submissions = self._fetch_in("assignment_submissions", "student_id", student_ids)
        notifications = self._fetch_in("notifications", "recipient_id", student_ids)

        course_ids = sorted(
            {
                int(r["course_id"])
                for r in enrollments + attendance
                if r.get("course_id") is not None
            }
        )
        submission_ids = sorted({int(r["id"]) for r in submissions if r.get("id") is not None})
        assignment_ids = sorted({int(r["assignment_id"]) for r in submissions if r.get("assignment_id") is not None})

        courses = self._fetch_in("courses", "id", course_ids)
        modules = self._fetch_in("modules", "course_id", course_ids)
        module_ids = sorted({int(r["id"]) for r in modules if r.get("id") is not None})
        materials = self._fetch_in("module_materials", "module_id", module_ids)

        if assignment_ids:
            assignments = self._fetch_in("assignments", "id", assignment_ids)
        else:
            assignments = self._fetch_in("assignments", "module_id", module_ids)

        answers = self._fetch_in("assignment_submission_answers", "submission_id", submission_ids)

        bundle["users"] = users
        bundle["course_enrollments"] = enrollments
        bundle["course_attendance"] = attendance
        bundle["assignment_submissions"] = submissions
        bundle["notifications"] = notifications
        bundle["courses"] = courses
        bundle["modules"] = modules
        bundle["module_materials"] = materials
        bundle["assignments"] = assignments
        bundle["assignment_submission_answers"] = answers
        return bundle


class MongoEventWriter:
    """Write event and feature documents into MongoDB."""

    def __init__(self) -> None:
        self._db = get_mongo_raw_db()

    @staticmethod
    def _chunks(documents: Sequence[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
        return [list(documents[index : index + size]) for index in range(0, len(documents), size)]

    async def insert_events(
        self,
        collection_name: str,
        documents: Iterable[dict[str, Any]],
        *,
        batch_size: int = 250,
        retry_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> int:
        payload = [doc for doc in documents]
        if not payload:
            return 0

        inserted = 0
        for batch in self._chunks(payload, batch_size):
            attempt = 0
            while True:
                try:
                    if all(doc.get("idempotency_key") for doc in batch):
                        for doc in batch:
                            await self._db[collection_name].replace_one(
                                {"idempotency_key": doc["idempotency_key"]},
                                doc,
                                upsert=True,
                            )
                        inserted += len(batch)
                    else:
                        result = await self._db[collection_name].insert_many(batch, ordered=False)
                        inserted += len(result.inserted_ids)
                    break
                except (AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError):
                    attempt += 1
                    if attempt >= retry_attempts:
                        raise
                    await asyncio.sleep(retry_delay_seconds * attempt)
        return inserted

    async def upsert_one(
        self,
        collection_name: str,
        filter_doc: dict[str, Any],
        document: dict[str, Any],
        *,
        retry_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        attempt = 0
        while True:
            try:
                await self._db[collection_name].replace_one(filter_doc, document, upsert=True)
                return
            except (AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError):
                attempt += 1
                if attempt >= retry_attempts:
                    raise
                await asyncio.sleep(retry_delay_seconds * attempt)


class Module4IngestionPlan:
    """High-level orchestration for the first ingestion pass."""

    def __init__(self) -> None:
        self.reader = SupabaseSourceReader()
        self.writer = MongoEventWriter()

    def build_raw_event_bundle(self, window: ExtractionWindow | None = None) -> dict[str, list[dict[str, Any]]]:
        return self.reader.fetch_source_bundle(window)

    async def bootstrap_history(self, window: ExtractionWindow | None = None) -> dict[str, int]:
        """Placeholder backfill flow.

        The next step is to convert each source table into the normalized Mongo
        event contracts defined in `contracts.py`.
        """

        source_bundle = self.reader.fetch_source_bundle(window)
        inserted: dict[str, int] = {}
        for table_name, rows in source_bundle.items():
            inserted[table_name] = len(rows)
        return inserted


class EventNormalizer:
    """Convert Supabase records into normalized Mongo event documents."""

    @staticmethod
    def _idempotency_key(prefix: str, *parts: Any) -> str:
        return "::".join([prefix, *[str(part) for part in parts if part is not None and str(part) != ""]])

    @staticmethod
    def _to_utc(value: Any) -> datetime:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        if value is None:
            return utc_now()
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def build_activity_login_event(self, row: dict[str, Any]) -> dict[str, Any]:
        user_id = row.get("user_id") or row.get("id")
        event_time = self._to_utc(row.get("created_at") or row.get("last_sign_in_at") or row.get("updated_at"))
        event = ActivityEvent(
            event_id=str(row.get("id") or self._idempotency_key("login", user_id, event_time.isoformat())),
            event_time=event_time,
            source="api",
            event_type="login",
            user_id=str(user_id),
            properties={
                "email": row.get("email"),
                "role_id": row.get("role_id"),
                "is_active": row.get("is_active"),
            },
            idempotency_key=self._idempotency_key("login", user_id, event_time.isoformat()),
        )
        return event.model_dump()

    def build_activity_event(self, row: dict[str, Any], event_type: str = "page_view") -> dict[str, Any]:
        event_time = self._to_utc(row.get("created_at") or row.get("updated_at") or row.get("event_time"))
        user_id = row.get("user_id") or row.get("student_id") or row.get("recipient_id") or row.get("lecturer_id") or row.get("created_by") or row.get("uploaded_by") or row.get("id")
        event = ActivityEvent(
            event_id=str(row.get("id") or self._idempotency_key(event_type, user_id, event_time.isoformat())),
            event_time=event_time,
            source="api",
            event_type=event_type,  # type: ignore[arg-type]
            user_id=str(user_id or ""),
            course_id=row.get("course_id"),
            module_id=row.get("module_id"),
            material_id=row.get("material_id") or row.get("id"),
            properties={
                "title": row.get("title") or row.get("course_code") or row.get("name"),
                "scenario": row.get("scenario"),
                "status": row.get("status"),
                "role_id": row.get("role_id"),
                "email": row.get("email"),
            },
            idempotency_key=self._idempotency_key(event_type, row.get("id"), event_time.isoformat()),
        )
        return event.model_dump()

    def build_content_event(self, row: dict[str, Any], event_type: str = "material_open") -> dict[str, Any]:
        event_time = self._to_utc(row.get("created_at") or row.get("updated_at"))
        event = ContentEvent(
            event_id=str(row.get("id") or self._idempotency_key(event_type, row.get("module_id"), row.get("uploaded_by"))),
            event_time=event_time,
            source="web",
            event_type=event_type,  # type: ignore[arg-type]
            user_id=str(row.get("uploaded_by") or row.get("user_id") or ""),
            course_id=row.get("course_id"),
            module_id=row.get("module_id"),
            material_id=row.get("id"),
            duration_sec=row.get("duration_sec"),
            properties={
                "name": row.get("name"),
                "description": row.get("description"),
                "file_url": row.get("file_url"),
                "mime_type": row.get("mime_type"),
            },
            idempotency_key=self._idempotency_key(event_type, row.get("id"), event_time.isoformat()),
        )
        return event.model_dump()

    def build_attendance_event(self, row: dict[str, Any]) -> dict[str, Any]:
        event_time = self._to_utc(row.get("session_date") or row.get("created_at"))
        status = str(row.get("status") or "")
        event_type = "session_attended" if status.lower() in {"present", "attended", "late"} else "session_absent"
        event = AttendanceEvent(
            event_id=str(row.get("id") or self._idempotency_key("attendance", row.get("student_id"), event_time.isoformat())),
            event_time=event_time,
            source="api",
            event_type=event_type,  # type: ignore[arg-type]
            user_id=str(row.get("student_id") or ""),
            course_id=row.get("course_id"),
            status=status or None,
            notes=row.get("notes"),
            properties={
                "status": row.get("status"),
                "notes": row.get("notes"),
                "recorded_by": row.get("recorded_by"),
            },
            idempotency_key=self._idempotency_key("attendance", row.get("id"), event_time.isoformat()),
        )
        return event.model_dump()

    def build_assessment_event(self, row: dict[str, Any]) -> dict[str, Any]:
        event_time = self._to_utc(row.get("created_at") or row.get("updated_at") or row.get("submitted_at"))
        timing_label = row.get("timing_label")
        status = str(row.get("status") or "")
        event_type = "submission_created"
        if status == "updated":
            event_type = "submission_updated"
        elif status == "graded":
            event_type = "graded"
        elif status in {"finalized", "graded_finalized"}:
            event_type = "graded_finalized"
        event = AssessmentEvent(
            event_id=str(row.get("id") or self._idempotency_key("assessment", row.get("student_id"), row.get("assignment_id"), event_time.isoformat())),
            event_time=event_time,
            source="api",
            event_type=event_type,  # type: ignore[arg-type]
            user_id=str(row.get("student_id") or ""),
            course_id=row.get("course_id"),
            assignment_id=row.get("assignment_id"),
            submission_id=row.get("submission_id") or row.get("id"),
            timing_label=timing_label,
            final_score=row.get("final_score"),
            properties={
                "assignment_id": row.get("assignment_id"),
                "submission_id": row.get("submission_id") or row.get("id"),
                "status": row.get("status"),
                "final_score": row.get("final_score"),
                "timing_label": timing_label,
            },
            idempotency_key=self._idempotency_key("assessment", row.get("id"), event_time.isoformat()),
        )
        return event.model_dump()

    def normalize_source_bundle(self, source_bundle: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
        """Normalize Supabase rows into Mongo event documents grouped by collection.

        This is the first bridge between transactional tables and the AI event
        layer. It intentionally keeps the mapping explicit so future changes to
        source tables do not silently leak into training data.
        """

        normalized: dict[str, list[dict[str, Any]]] = {
            "activity_events": [],
            "assessment_events": [],
            "attendance_events": [],
            "content_events": [],
        }

        for source_name, rows in source_bundle.items():
            collection_name = RAW_EVENT_COLLECTION_BY_SOURCE.get(source_name)
            if not collection_name:
                continue

            for row in rows:
                if source_name == "users":
                    normalized[collection_name].append(self.build_activity_login_event(row))
                elif source_name in {"module_materials", "modules"}:
                    normalized[collection_name].append(self.build_content_event(row))
                elif source_name == "course_attendance":
                    normalized[collection_name].append(self.build_attendance_event(row))
                elif source_name in {"assignments", "assignment_submissions", "assignment_submission_answers"}:
                    normalized[collection_name].append(self.build_assessment_event(row))
                elif source_name in {"courses", "course_enrollments", "notifications"}:
                    normalized[collection_name].append(self.build_activity_event(row, event_type="page_view"))

        return normalized
