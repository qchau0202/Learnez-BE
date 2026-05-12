"""Seed plausible weekly analytics for a single real student.

For every (course, week) inside the course window, upserts a
deterministic feature snapshot into ``learnez_ai.student_weekly_features``
keyed on ``(user_id, course_id, week_start)`` so re-runs are idempotent.
Three personas: ``steady`` (default), ``thriving``, ``at_risk``.

Usage::

    cd BE
    python -m ml.data.students.features --email student1@email.com --weeks 16
    python -m ml.data.students.features --email student1@email.com --persona at_risk
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.core.database import get_mongo_ai_db, get_supabase  # noqa: E402

PersonaName = str

# Per-persona feature ranges. Scores live on the TDTU 0-10 scale.
PERSONAS: dict[PersonaName, dict[str, Any]] = {
    "steady": {
        "logins_range": (5, 11),
        "active_minutes_range": (90, 220),
        "materials_range": (4, 11),
        "material_open_sec_range": (1800, 6000),
        "submissions_per_week": (0, 3),
        "late_share_range": (0.0, 0.2),
        "attendance_rate_range": (0.78, 0.94),
        "absence_per_week_range": (0, 1),
        "avg_score_range": (6.8, 8.6),
        "score_jitter": 0.6,
    },
    "thriving": {
        "logins_range": (8, 16),
        "active_minutes_range": (160, 320),
        "materials_range": (8, 18),
        "material_open_sec_range": (3500, 9000),
        "submissions_per_week": (1, 4),
        "late_share_range": (0.0, 0.05),
        "attendance_rate_range": (0.92, 1.0),
        "absence_per_week_range": (0, 0),
        "avg_score_range": (8.2, 9.6),
        "score_jitter": 0.4,
    },
    "at_risk": {
        "logins_range": (0, 5),
        "active_minutes_range": (5, 80),
        "materials_range": (0, 4),
        "material_open_sec_range": (0, 1500),
        "submissions_per_week": (0, 2),
        "late_share_range": (0.3, 0.7),
        "attendance_rate_range": (0.40, 0.65),
        "absence_per_week_range": (1, 3),
        "avg_score_range": (3.8, 6.0),
        "score_jitter": 0.8,
    },
}


def _week_floor_utc(d: datetime | date) -> datetime:
    """Snap any date/datetime to Monday-00:00:00-UTC of its week."""
    if isinstance(d, date) and not isinstance(d, datetime):
        d = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    elif d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    else:
        d = d.astimezone(timezone.utc)
    monday = d - timedelta(days=d.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)


def _resolve_student(sb, email: str) -> dict[str, Any]:
    rows = (
        sb.table("users")
        .select("user_id, full_name, email, role_id")
        .eq("email", email)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        raise SystemExit(f"No user found with email={email}")
    user = rows[0]
    role_id = user.get("role_id")
    if role_id != 3:
        print(f"Warning: user {email} has role_id={role_id} (expected 3 for student).")
    return user


def _resolve_courses(sb, user_id: str) -> list[dict[str, Any]]:
    enr = (
        sb.table("course_enrollments")
        .select("course_id")
        .eq("student_id", user_id)
        .execute()
        .data
        or []
    )
    course_ids = sorted({int(r["course_id"]) for r in enr if r.get("course_id") is not None})
    if not course_ids:
        return []
    out: list[dict[str, Any]] = []
    for i in range(0, len(course_ids), 200):
        batch = course_ids[i : i + 200]
        rows = (
            sb.table("courses")
            .select(
                "id, course_code, title, semester, academic_year, "
                "course_start_date, course_end_date, "
                "course_session, course_session_date, course_session_duration, "
                "course_occurences, lecturer_id"
            )
            .in_("id", batch)
            .execute()
            .data
            or []
        )
        out.extend(rows)
    out.sort(key=lambda r: int(r.get("id") or 0))
    return out


def _course_window_weeks(
    course: dict[str, Any], *, fallback_start: datetime, fallback_end: datetime,
) -> list[datetime]:
    """Return week_start datetimes inside the course window (intersected with fallback)."""
    raw_start = course.get("course_start_date")
    raw_end = course.get("course_end_date")
    start: datetime
    end: datetime
    if raw_start:
        start = datetime.fromisoformat(str(raw_start))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    else:
        start = fallback_start
    if raw_end:
        end = datetime.fromisoformat(str(raw_end))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
    else:
        end = fallback_end
    start = max(start, fallback_start)
    end = min(end, fallback_end)
    if start >= end:
        return []
    weeks: list[datetime] = []
    cursor = _week_floor_utc(start)
    end_floor = _week_floor_utc(end)
    while cursor <= end_floor:
        weeks.append(cursor)
        cursor += timedelta(days=7)
    return weeks


def _seeded_rng(*parts: Any) -> random.Random:
    """Deterministic RNG so re-runs produce stable demo numbers."""
    digest = hashlib.sha1(":".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def _generate_features(
    *, persona: dict[str, Any], week_index: int, total_weeks: int, rng: random.Random,
) -> dict[str, Any]:
    """Make one weekly feature dict consistent with ``FeatureInputSummary``.

    At-risk persona drifts downward over the term; thriving drifts up slightly.
    """
    progress = week_index / max(total_weeks - 1, 1)

    drift = 1.0
    if persona is PERSONAS["at_risk"]:
        drift = 1.0 - 0.4 * progress
    elif persona is PERSONAS["thriving"]:
        drift = 1.0 + 0.05 * progress

    def _scaled_int(rng_: random.Random, lo: int, hi: int) -> int:
        return max(0, int(round(rng_.randint(lo, hi) * drift)))

    def _scaled_float(rng_: random.Random, lo: float, hi: float) -> float:
        return max(0.0, rng_.uniform(lo, hi) * drift)

    logins = _scaled_int(rng, *persona["logins_range"])
    active_minutes = round(_scaled_float(rng, *persona["active_minutes_range"]), 2)
    materials_viewed = _scaled_int(rng, *persona["materials_range"])
    material_open_time_sec = round(_scaled_float(rng, *persona["material_open_sec_range"]), 2)

    subs_total = rng.randint(*persona["submissions_per_week"])
    late_share = rng.uniform(*persona["late_share_range"])
    subs_late = int(round(subs_total * late_share))
    subs_on_time = max(subs_total - subs_late, 0)

    attendance_rate = round(rng.uniform(*persona["attendance_rate_range"]) * drift, 4)
    attendance_rate = max(0.0, min(1.0, attendance_rate))
    absence_count = rng.randint(*persona["absence_per_week_range"])

    avg_score_lo, avg_score_hi = persona["avg_score_range"]
    centre = rng.uniform(avg_score_lo, avg_score_hi)
    # Clamp to 0-10 TDTU scale.
    avg_score = max(
        0.0, min(10.0, centre + rng.gauss(0, persona["score_jitter"]) * (1.5 - drift))
    )

    inactivity_streak_days = 0
    if persona is PERSONAS["at_risk"] and rng.random() < 0.4:
        inactivity_streak_days = rng.randint(3, 9)

    return {
        "logins": int(logins),
        "active_minutes": float(active_minutes),
        "materials_viewed": int(materials_viewed),
        "material_open_time_sec": float(material_open_time_sec),
        "submissions_total": int(subs_total),
        "submissions_on_time": int(subs_on_time),
        "submissions_late": int(subs_late),
        "attendance_rate": float(attendance_rate),
        "absence_count": int(absence_count),
        "inactivity_streak_days": int(inactivity_streak_days),
        "avg_score_30d": round(float(avg_score), 2),
        "score_trend_30d": None,
    }


async def seed_student_features(
    *,
    user_id: str,
    courses: list[dict[str, Any]],
    persona_name: str,
    weeks: int = 16,
    ignore_course_window: bool = False,
    log_prefix: str = "",
) -> int:
    """Upsert weekly snapshots for one student/persona. Returns count written."""
    persona = PERSONAS.get(persona_name)
    if persona is None:
        raise ValueError(f"Unknown persona '{persona_name}'. Choose: {', '.join(PERSONAS)}")

    end = datetime.now(timezone.utc)
    end = _week_floor_utc(end) + timedelta(days=7)
    fallback_start = _week_floor_utc(end - timedelta(weeks=weeks))

    db = get_mongo_ai_db()
    col = db["student_weekly_features"]
    await col.create_index(
        [("user_id", 1), ("course_id", 1), ("week_start", 1)],
        unique=True,
        name="user_id_1_course_id_1_week_start_1",
    )

    grand_total = 0
    for course in courses:
        course_id = int(course["id"])
        if ignore_course_window:
            week_dates: list[datetime] = []
            cursor = fallback_start
            while cursor < end:
                week_dates.append(cursor)
                cursor += timedelta(days=7)
        else:
            week_dates = _course_window_weeks(
                course, fallback_start=fallback_start, fallback_end=end,
            )
        if not week_dates:
            if log_prefix:
                print(f"{log_prefix}[skip] course {course_id}: no overlap with look-back window")
            continue
        for idx, week_start in enumerate(week_dates):
            rng = _seeded_rng(persona_name, user_id, course_id, week_start.isoformat())
            features = _generate_features(
                persona=persona, week_index=idx, total_weeks=len(week_dates), rng=rng,
            )
            doc = {
                "user_id": user_id,
                "course_id": course_id,
                "week_start": week_start,
                "week_end": week_start + timedelta(days=7),
                "source_event_max_time": week_start + timedelta(days=6, hours=20),
                "schema_version": 1,
                "updated_at": datetime.now(timezone.utc),
                "features": features,
            }
            await col.replace_one(
                {"user_id": user_id, "course_id": course_id, "week_start": week_start},
                doc,
                upsert=True,
            )
            grand_total += 1
        if log_prefix:
            print(f"{log_prefix}[ok] course {course_id}: upserted {len(week_dates)} snapshot(s)")

    return grand_total


async def _seed(args: argparse.Namespace) -> int:
    sb = get_supabase(service_role=True)
    if sb is None:
        raise SystemExit("Supabase service-role client not configured (SUPABASE_SERVICE_ROLE_KEY).")

    user = _resolve_student(sb, args.email)
    user_id = str(user["user_id"])
    print(f"Seeding analytics for {user.get('full_name') or args.email} ({user_id})")

    courses = _resolve_courses(sb, user_id)
    if not courses:
        raise SystemExit(f"User {args.email} has no course_enrollments rows; nothing to seed.")

    print(f"Found {len(courses)} enrolled course(s):")
    for c in courses:
        print(
            f"  · {c.get('course_code') or '?'} — {c.get('title')} "
            f"[{c.get('course_start_date') or 'no-start'} → {c.get('course_end_date') or 'no-end'}]"
        )

    grand_total = await seed_student_features(
        user_id=user_id,
        courses=courses,
        persona_name=args.persona,
        weeks=args.weeks,
        ignore_course_window=args.ignore_course_window,
        log_prefix="  ",
    )

    print(
        f"\nDone. Upserted {grand_total} snapshot(s) into "
        f"learnez_ai.student_weekly_features for {args.email}."
    )
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed demo weekly features for one student.")
    p.add_argument("--email", required=True)
    p.add_argument("--weeks", type=int, default=16,
                   help="Look-back window in weeks (default 16). Intersected with each course window.")
    p.add_argument("--persona", choices=sorted(PERSONAS.keys()), default="steady")
    p.add_argument("--ignore-course-window", action="store_true",
                   help="Seed every week in the look-back even before the course's start date.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(_seed(args)))


if __name__ == "__main__":
    main()
