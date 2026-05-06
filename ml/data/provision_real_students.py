#!/usr/bin/env python3
"""Provision real students in Supabase and sync their analytics into Mongo.

Creates 10-15 student accounts, enrolls them in existing courses, seeds attendance
and submissions, then syncs student-scoped events into Mongo + weekly features.

Usage (from BE/, venv on):
  python -m ml.data.provision_real_students --count 12
"""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.core.database import get_supabase
from app.core.database import get_mongo_raw_db
from ml.data.feature_jobs import WeeklyFeatureAggregator
from ml.data.supabase_sample_sync import sync_live_sample


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create real students in Supabase and map them into Mongo analytics.")
    p.add_argument("--count", type=int, default=12, help="Number of students to create (recommended 10-15).")
    p.add_argument("--password", type=str, default="123456", help="Default password for created auth users.")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--course-id", type=int, default=0, help="Optional fixed course_id to enroll all students.")
    p.add_argument(
        "--no-auth",
        action="store_true",
        help="Skip auth user creation (only if your schema does not require auth FK).",
    )
    p.add_argument("--preview-only", action="store_true", help="Create no data; only print what would run.")
    return p.parse_args()


def _svc():
    svc = get_supabase(service_role=True)
    if not svc:
        raise RuntimeError("Missing SUPABASE_SERVICE_ROLE_KEY / Supabase service role configuration.")
    return svc


def _pick_courses(svc, count: int, course_id: int | None, rng: random.Random) -> list[dict[str, Any]]:
    if course_id:
        rows = svc.table("courses").select("id,title").eq("id", course_id).limit(1).execute().data or []
        if not rows:
            raise RuntimeError(f"Course {course_id} not found.")
        return rows
    rows = svc.table("courses").select("id,title").limit(max(5, count)).execute().data or []
    if not rows:
        raise RuntimeError("No courses found in Supabase.")
    rng.shuffle(rows)
    return rows


def _create_student_account(
    svc,
    *,
    idx: int,
    password: str,
    with_auth: bool,
    faculty_id: int | None,
    department_id: int | None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d%H%M%S")
    email = f"real.student.{stamp}.{idx}@learnez.local"
    full_name = f"Real Student {idx:02d}"

    if not with_auth:
        # Most deployments keep users.user_id -> auth.users.id FK.
        # Keep this explicit so operators do not accidentally create broken rows.
        raise RuntimeError("no-auth mode is not supported with current schema; use default auth-backed provisioning.")
    auth_res = svc.auth.admin.create_user({"email": email, "password": password, "email_confirm": True})
    if not auth_res.user:
        raise RuntimeError(f"Failed to create auth user for {email}")
    uid = auth_res.user.id

    svc.table("users").insert(
        {
            "user_id": uid,
            "email": email,
            "full_name": full_name,
            "role_id": 3,
            "is_active": True,
        }
    ).execute()
    svc.table("student_profiles").upsert(
        {
            "user_id": uid,
            "student_id": f"S{stamp[-6:]}{idx:02d}",
            "major": "Computer Science",
            "enrolled_year": now.year - 1,
            "current_gpa": round(5.5 + (idx % 10) * 0.35, 2),
            "cumulative_gpa": round(5.7 + (idx % 10) * 0.33, 2),
            "faculty_id": faculty_id,
            "department_id": department_id,
            "class": f"K{(idx % 3) + 1}",
        }
    ).execute()
    return {"user_id": uid, "email": email, "full_name": full_name}


def _seed_student_learning_data(
    svc,
    *,
    student_id: str,
    course_id: int,
    rng: random.Random,
) -> None:
    svc.table("course_enrollments").upsert({"course_id": course_id, "student_id": student_id}).execute()

    # Attendance for recent weeks
    statuses = ["Present", "Present", "Late", "Absent"]
    for weeks_ago in range(0, 8):
        session_date = (datetime.now(timezone.utc) - timedelta(weeks=weeks_ago)).replace(hour=0, minute=0, second=0, microsecond=0)
        svc.table("course_attendance").insert(
            {
                "course_id": course_id,
                "student_id": student_id,
                "status": rng.choice(statuses),
                "session_date": session_date.isoformat(),
                "notes": "Auto-seeded for analytics bootstrap.",
            }
        ).execute()

    # Submissions mapped to assignments in course modules
    modules = svc.table("modules").select("id").eq("course_id", course_id).execute().data or []
    module_ids = [m["id"] for m in modules if m.get("id") is not None]
    if not module_ids:
        return
    assignments = svc.table("assignments").select("id,module_id").in_("module_id", module_ids).limit(20).execute().data or []
    for a in assignments[: min(6, len(assignments))]:
        score = round(rng.uniform(45, 95), 2)
        status = "graded" if score >= 55 else "submitted"
        svc.table("assignment_submissions").upsert(
            {
                "student_id": student_id,
                "assignment_id": a["id"],
                "status": status,
                "is_corrected": status == "graded",
                "final_score": score if status == "graded" else None,
            }
        ).execute()

    # Optional notification signal (schema differs across deployments; skip if unsupported).
    try:
        svc.table("notifications").insert(
            {
                "recipient_id": student_id,
                "course_id": course_id,
                "title": "Learning reminder",
                "content": "Keep up your weekly activities and submissions.",
                "type": "system",
                "scenario": "learning_reminder",
            }
        ).execute()
    except Exception:
        pass


async def main_async() -> int:
    args = parse_args()
    rng = random.Random(args.seed)
    svc = _svc()

    count = max(10, min(15, int(args.count)))
    departments = svc.table("departments").select("id,from_faculty").limit(10).execute().data or []
    dep = departments[0] if departments else {}
    faculty_id = dep.get("from_faculty")
    department_id = dep.get("id")
    courses = _pick_courses(svc, count, args.course_id if args.course_id > 0 else None, rng)

    print(f"provision_count: {count}")
    print(f"target_courses: {[c.get('id') for c in courses]}")
    if args.preview_only:
        print("preview_only: no inserts executed")
        return 0

    created_ids: list[str] = []
    student_course_pairs: list[tuple[str, int]] = []
    for i in range(count):
        print(f"creating_student: {i + 1}/{count}", flush=True)
        created = _create_student_account(
            svc,
            idx=i + 1,
            password=args.password,
            with_auth=not args.no_auth,
            faculty_id=faculty_id,
            department_id=department_id,
        )
        chosen_course = courses[i % len(courses)]
        chosen_course_id = int(chosen_course["id"])
        _seed_student_learning_data(svc, student_id=created["user_id"], course_id=chosen_course_id, rng=rng)
        created_ids.append(created["user_id"])
        student_course_pairs.append((created["user_id"], chosen_course_id))

    print(f"created_students: {len(created_ids)}")
    print("syncing_student_ids_to_mongo...")
    report = await sync_live_sample(limit_per_table=50, preview_only=False, student_ids=created_ids)
    print(f"write_counts: {report['write_counts']}")
    print(f"refreshed_weekly_snapshots: {report['refreshed_weekly_snapshots']}")

    # Seed extra behavior signals directly into Mongo raw events for richer weekly features.
    raw_db = get_mongo_raw_db()
    now = datetime.now(timezone.utc)
    activity_docs: list[dict[str, Any]] = []
    for sid, cid in student_course_pairs:
        for w in range(0, 8):
            et = (now - timedelta(weeks=w)).replace(hour=9, minute=0, second=0, microsecond=0)
            activity_docs.extend(
                [
                    {
                        "event_id": f"seed-login-{uuid4().hex}",
                        "event_type": "login",
                        "event_time": et,
                        "created_at": et,
                        "source": "job",
                        "schema_version": 1,
                        "user_id": sid,
                        "course_id": cid,
                        "properties": {"seeded": True},
                        "idempotency_key": f"seed-login::{sid}::{cid}::{et.isoformat()}",
                    },
                    {
                        "event_id": f"seed-page-{uuid4().hex}",
                        "event_type": "page_view",
                        "event_time": et + timedelta(minutes=5),
                        "created_at": et + timedelta(minutes=5),
                        "source": "job",
                        "schema_version": 1,
                        "user_id": sid,
                        "course_id": cid,
                        "duration_sec": 900,
                        "properties": {"seeded": True, "page": "course_dashboard"},
                        "idempotency_key": f"seed-page::{sid}::{cid}::{(et + timedelta(minutes=5)).isoformat()}",
                    },
                    {
                        "event_id": f"seed-heartbeat-{uuid4().hex}",
                        "event_type": "session_heartbeat",
                        "event_time": et + timedelta(minutes=20),
                        "created_at": et + timedelta(minutes=20),
                        "source": "job",
                        "schema_version": 1,
                        "user_id": sid,
                        "course_id": cid,
                        "duration_sec": 1200,
                        "properties": {"seeded": True},
                        "idempotency_key": f"seed-heartbeat::{sid}::{cid}::{(et + timedelta(minutes=20)).isoformat()}",
                    },
                ]
            )
    if activity_docs:
        await raw_db["activity_events"].insert_many(activity_docs, ordered=False)

    # Refresh feature layer for the seeded timeline
    agg = WeeklyFeatureAggregator()
    refreshed_extra = await agg.persist_weekly_snapshots_for_range(now - timedelta(weeks=8), now)
    print(f"refreshed_weekly_snapshots_extra: {refreshed_extra}")
    print(f"student_ids: {','.join(created_ids)}")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
