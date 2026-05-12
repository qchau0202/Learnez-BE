"""Seed persona-aware ``course_attendance`` rows for one or more students.

Idempotent: reads existing ``(student_id, course_id)`` rows first and
only inserts session_dates not already present. Never deletes attendance.

Usage::

    cd BE
    python -m ml.data.students.attendance --email student1@email.com --persona steady
    python -m ml.data.students.attendance --email student1@email.com --persona at_risk --max-sessions 32
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.core.database import get_supabase  # noqa: E402

from .features import PERSONAS, _resolve_courses, _resolve_student, _seeded_rng  # noqa: E402


# Per-persona attendance distribution: shares of present / late / absent.
ATTENDANCE_DISTRIBUTIONS: dict[str, tuple[float, float, float]] = {
    "thriving": (0.96, 0.03, 0.01),
    "steady": (0.86, 0.07, 0.07),
    "at_risk": (0.55, 0.13, 0.32),
}

WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


def _parse_session_weekdays(course: dict[str, Any]) -> list[int]:
    """Resolve which weekdays the course meets on (Monday=0..Sunday=6)."""
    candidates = []
    raw = (course.get("course_session_date") or course.get("course_session") or "").strip()
    if not raw:
        return [0]
    for token in raw.replace(";", ",").split(","):
        key = token.strip().lower()
        if key in WEEKDAY_NAMES:
            candidates.append(WEEKDAY_NAMES[key])
    return sorted(set(candidates)) or [0]


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def _generate_session_dates(
    *, course: dict[str, Any], today_utc: date, max_sessions: int,
) -> list[date]:
    """Return session dates that have already happened (never future)."""
    weekdays = _parse_session_weekdays(course)
    raw_start = _coerce_date(course.get("course_start_date"))
    raw_end = _coerce_date(course.get("course_end_date"))
    if raw_start is None:
        raw_start = today_utc - timedelta(weeks=16)
    course_end = raw_end or (raw_start + timedelta(weeks=20))
    upper = min(course_end, today_utc)
    if upper < raw_start:
        return []
    sessions: list[date] = []
    cursor = raw_start
    while cursor <= upper and len(sessions) < max_sessions:
        if cursor.weekday() in weekdays:
            sessions.append(cursor)
        cursor += timedelta(days=1)
    return sessions


def _pick_status(rng_uniform: float, dist: tuple[float, float, float]) -> str:
    present, late, _absent = dist
    if rng_uniform < present:
        return "present"
    if rng_uniform < present + late:
        return "late"
    return "absent"


def seed_attendance_for_student(
    sb,
    *,
    user_id: str,
    courses: list[dict[str, Any]],
    persona_name: str,
    max_sessions_per_course: int = 32,
    log_prefix: str = "",
) -> int:
    """Insert missing attendance rows for one student across their courses."""
    if persona_name not in PERSONAS:
        raise ValueError(f"Unknown persona '{persona_name}'.")
    dist = ATTENDANCE_DISTRIBUTIONS.get(persona_name, ATTENDANCE_DISTRIBUTIONS["steady"])
    today_utc = datetime.now(timezone.utc).date()

    inserted = 0
    for course in courses:
        course_id = int(course["id"])
        recorded_by = course.get("lecturer_id")
        sessions = _generate_session_dates(
            course=course, today_utc=today_utc, max_sessions=max_sessions_per_course,
        )
        if not sessions:
            if log_prefix:
                print(f"{log_prefix}[skip] attendance course {course_id}: no past sessions")
            continue

        existing_rows = (
            sb.table("course_attendance")
            .select("session_date")
            .eq("student_id", user_id)
            .eq("course_id", course_id)
            .execute()
            .data
            or []
        )
        existing_dates: set[date] = set()
        for row in existing_rows:
            d = _coerce_date(row.get("session_date"))
            if d is not None:
                existing_dates.add(d)

        new_payload: list[dict[str, Any]] = []
        for session_dt in sessions:
            if session_dt in existing_dates:
                continue
            rng = _seeded_rng(persona_name, user_id, course_id, "attendance", session_dt.isoformat())
            status = _pick_status(rng.random(), dist)
            session_iso = datetime(
                session_dt.year, session_dt.month, session_dt.day, 9, 0, 0,
                tzinfo=timezone.utc,
            ).isoformat()
            new_payload.append(
                {
                    "student_id": user_id,
                    "course_id": course_id,
                    "session_date": session_iso,
                    "status": status,
                    "recorded_by": recorded_by,
                    "notes": None,
                }
            )

        if not new_payload:
            if log_prefix:
                print(f"{log_prefix}[ok ] attendance course {course_id}: already up to date")
            continue

        for i in range(0, len(new_payload), 200):
            batch = new_payload[i : i + 200]
            sb.table("course_attendance").insert(batch).execute()
            inserted += len(batch)

        if log_prefix:
            print(
                f"{log_prefix}[ok ] attendance course {course_id}: "
                f"+{len(new_payload)} new sessions (existing: {len(existing_dates)})"
            )

    return inserted


def _seed_cli(args: argparse.Namespace) -> int:
    sb = get_supabase(service_role=True)
    if sb is None:
        raise SystemExit("Supabase service-role client not configured.")
    user = _resolve_student(sb, args.email)
    user_id = str(user["user_id"])
    courses = _resolve_courses(sb, user_id)
    if not courses:
        raise SystemExit(f"User {args.email} has no course_enrollments rows.")
    print(f"Seeding attendance for {user.get('full_name') or args.email} ({user_id})")
    inserted = seed_attendance_for_student(
        sb, user_id=user_id, courses=courses, persona_name=args.persona,
        max_sessions_per_course=args.max_sessions, log_prefix="  ",
    )
    print(f"\nInserted {inserted} attendance row(s).")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed demo attendance rows for one student.")
    p.add_argument("--email", required=True)
    p.add_argument("--persona", choices=sorted(PERSONAS.keys()), default="steady")
    p.add_argument("--max-sessions", type=int, default=32,
                   help="Cap sessions per course (default 32).")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(asyncio.to_thread(_seed_cli, args)))


if __name__ == "__main__":
    main()
