"""Feature aggregation jobs for the first AI training snapshot.

The first job in this module creates a weekly per-student feature summary from
the raw Mongo event collections. The output is the primary input for the
baseline dropout-risk model.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.database import get_mongo_ai_db, get_mongo_raw_db

from .contracts import FeatureInputSummary, StudentWeeklyFeatureSnapshot


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def week_bounds(reference: datetime | None = None) -> tuple[datetime, datetime]:
    anchor = (reference or utc_now()).astimezone(timezone.utc)
    start = anchor - timedelta(days=anchor.weekday())
    start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    return start, end


class WeeklyFeatureAggregator:
    """Aggregate Mongo event streams into weekly training snapshots."""

    def __init__(self) -> None:
        self._raw_db = get_mongo_raw_db()
        self._db = get_mongo_ai_db()

    async def load_window_events(self, week_start: datetime, week_end: datetime) -> list[dict[str, Any]]:
        collections = ["activity_events", "assessment_events", "attendance_events", "content_events"]
        events: list[dict[str, Any]] = []
        for name in collections:
            cursor = self._raw_db[name].find({"event_time": {"$gte": week_start, "$lt": week_end}})
            events.extend(await cursor.to_list(length=None))
        return events

    @staticmethod
    def _summarize_user(events: list[dict[str, Any]], week_start: datetime, week_end: datetime) -> FeatureInputSummary:
        user_id = str(events[0].get("user_id"))
        course_id = next((event.get("course_id") for event in events if event.get("course_id") is not None), None)
        logins = 0
        active_minutes = 0.0
        materials_viewed = 0
        material_open_time_sec = 0.0
        submissions_total = 0
        submissions_on_time = 0
        submissions_late = 0
        attendance_hits = 0
        attendance_possible = 0
        absence_count = 0
        score_values: list[float] = []
        source_event_max_time = None

        for event in events:
            event_time = event.get("event_time")
            if isinstance(event_time, datetime) and (source_event_max_time is None or event_time > source_event_max_time):
                source_event_max_time = event_time
            event_type = event.get("event_type")
            properties = event.get("properties") or {}
            if event_type == "login":
                logins += 1
            if event_type == "material_open":
                materials_viewed += 1
                duration = event.get("duration_sec")
                if isinstance(duration, (int, float)):
                    material_open_time_sec += float(duration)
                    active_minutes += float(duration) / 60.0
            # Simulated LMS uses page_view (not material_open); count as lightweight engagement.
            if event_type == "page_view":
                materials_viewed += 1
            if event_type == "session_heartbeat":
                duration = event.get("duration_sec")
                if isinstance(duration, (int, float)):
                    active_minutes += float(duration) / 60.0
            if event_type == "submission_created":
                submissions_total += 1
                timing_label = event.get("timing_label") or properties.get("timing_label")
                if timing_label == "late":
                    submissions_late += 1
                else:
                    submissions_on_time += 1
                final_score = event.get("final_score")
                if isinstance(final_score, (int, float)):
                    score_values.append(float(final_score))
            # Legacy / alternate shapes
            elif event.get("submission_id") or properties.get("submission_id"):
                if event_type not in {"graded", "graded_finalized"}:
                    submissions_total += 1
                    timing_label = event.get("timing_label") or properties.get("timing_label")
                    if timing_label == "late":
                        submissions_late += 1
                    else:
                        submissions_on_time += 1
            if event_type in {"session_attended", "session_absent", "attendance_marked", "attendance_updated"}:
                attendance_possible += 1
                status_raw = event.get("status") or properties.get("status") or ""
                status_norm = str(status_raw).strip().lower()
                if event_type == "session_absent" or status_norm == "absent":
                    absence_count += 1
                else:
                    attendance_hits += 1
            if event_type == "page_view" and properties.get("status"):
                attendance_possible += 1
                if properties.get("status") == "present":
                    attendance_hits += 1
                else:
                    absence_count += 1
            final_score = properties.get("final_score")
            if event_type != "submission_created" and isinstance(final_score, (int, float)):
                score_values.append(float(final_score))

        attendance_rate = (attendance_hits / attendance_possible) if attendance_possible else None
        avg_score_30d = None
        if score_values:
            raw_avg = sum(score_values) / len(score_values)
            # Simulation / LMS often uses 0–10; training proxy in dataset_builder expects ~0–100 scale.
            if max(score_values) <= 10.5:
                avg_score_30d = raw_avg * 10.0
            else:
                avg_score_30d = raw_avg

        return FeatureInputSummary(
            user_id=user_id,
            course_id=course_id,
            week_start=week_start,
            week_end=week_end,
            logins=logins,
            active_minutes=round(active_minutes, 2),
            materials_viewed=materials_viewed,
            material_open_time_sec=round(material_open_time_sec, 2),
            submissions_total=submissions_total,
            submissions_on_time=submissions_on_time,
            submissions_late=submissions_late,
            attendance_rate=round(attendance_rate, 4) if attendance_rate is not None else None,
            absence_count=absence_count,
            inactivity_streak_days=0,
            avg_score_30d=round(float(avg_score_30d), 4) if avg_score_30d is not None else None,
            score_trend_30d=None,
            source_event_max_time=source_event_max_time,
        )

    async def build_weekly_snapshots(self, reference: datetime | None = None) -> list[StudentWeeklyFeatureSnapshot]:
        week_start, week_end = week_bounds(reference)
        events = await self.load_window_events(week_start, week_end)
        grouped: dict[tuple[str, int | None], list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            user_id = event.get("user_id")
            if not user_id:
                continue
            course_id = event.get("course_id")
            grouped[(str(user_id), int(course_id) if isinstance(course_id, int) else None)].append(event)

        snapshots: list[StudentWeeklyFeatureSnapshot] = []
        for (user_id, course_id), user_events in grouped.items():
            summary = self._summarize_user(user_events, week_start, week_end)
            snapshots.append(
                StudentWeeklyFeatureSnapshot(
                    user_id=user_id,
                    course_id=course_id,
                    week_start=week_start,
                    week_end=week_end,
                    source_event_max_time=summary.source_event_max_time,
                    features=summary.model_dump(exclude={"user_id", "course_id", "week_start", "week_end", "source_event_max_time", "schema_version", "updated_at"}),
                )
            )
        return snapshots

    async def persist_weekly_snapshots(self, reference: datetime | None = None) -> int:
        snapshots = await self.build_weekly_snapshots(reference)
        if not snapshots:
            return 0
        collection = self._db["student_weekly_features"]
        for snapshot in snapshots:
            await collection.replace_one(
                {"user_id": snapshot.user_id, "course_id": snapshot.course_id, "week_start": snapshot.week_start},
                snapshot.model_dump(),
                upsert=True,
            )
        return len(snapshots)

    async def persist_weekly_snapshots_for_range(self, start: datetime, end: datetime) -> int:
        """Backfill weekly snapshots across an inclusive date range."""

        cursor = week_bounds(start)[0]
        end_boundary = week_bounds(end)[0]
        total = 0
        while cursor <= end_boundary:
            total += await self.persist_weekly_snapshots(reference=cursor)
            cursor += timedelta(days=7)
        return total
