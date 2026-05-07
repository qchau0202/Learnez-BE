#!/usr/bin/env python3
"""Audit the *real* cohort across Supabase + Mongo.

The point of this script is to answer: "Is my data ready for analytics?"
i.e. do the students that exist in Supabase have:
  1. a ``student_profiles`` row with faculty_id, department_id, and class
  2. at least one ``course_enrollments`` row
  3. at least one ``student_weekly_features`` row in Mongo

It also reports how much of MongoDB belongs to **simulation** user_ids that
don't exist in Supabase ``users`` at all — those are the rows the cleanup
script can safely delete.

Usage (from BE/, venv on):

  python -m ml.data.audit_real_cohort
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from app.core.database import get_mongo_ai_db, get_mongo_raw_db, get_supabase


@dataclass(slots=True)
class CohortAudit:
    real_user_count: int = 0
    real_student_count: int = 0
    students_with_profile: int = 0
    students_with_class: int = 0
    students_with_faculty: int = 0
    students_with_department: int = 0
    students_with_enrollment: int = 0
    courses_total: int = 0
    courses_with_lecturer: int = 0
    courses_with_enrollment: int = 0
    enrollment_rows: int = 0


def _supabase():
    sb = get_supabase(service_role=True)
    if not sb:
        raise RuntimeError("Missing SUPABASE_SERVICE_ROLE_KEY; cannot audit Supabase.")
    return sb


def _read_supabase() -> dict[str, Any]:
    sb = _supabase()
    users = sb.table("users").select("user_id, role_id, is_active").execute().data or []
    profiles = (
        sb.table("student_profiles")
        .select("user_id, student_id, class, faculty_id, department_id")
        .execute()
        .data
        or []
    )
    enrollments = sb.table("course_enrollments").select("student_id, course_id").execute().data or []
    courses = sb.table("courses").select("id, lecturer_id, course_code, class_room").execute().data or []
    return {
        "users": users,
        "profiles": profiles,
        "enrollments": enrollments,
        "courses": courses,
    }


async def _mongo_stats(real_user_ids: set[str]) -> dict[str, dict[str, int]]:
    raw_db = get_mongo_raw_db()
    ai_db = get_mongo_ai_db()
    out: dict[str, dict[str, int]] = {}
    raw_collections = [
        "activity_events",
        "assessment_events",
        "attendance_events",
        "content_events",
    ]
    ai_collections = [
        "student_weekly_features",
        "student_daily_features",
        "course_engagement_features",
        "risk_scores",
    ]

    async def _check(db, name: str) -> dict[str, int]:
        coll = db[name]
        total = await coll.count_documents({})
        if total == 0:
            return {"total": 0, "real": 0, "simulation": 0, "no_user_id": 0}
        # Sample 5k user_ids to avoid streaming the whole collection.
        sampled_users: set[str] = set()
        no_uid = 0
        cursor = coll.find({}, {"user_id": 1, "_id": 0}).limit(5000)
        async for d in cursor:
            uid = d.get("user_id")
            if uid is None:
                no_uid += 1
                continue
            sampled_users.add(str(uid))
        real = sum(1 for u in sampled_users if u in real_user_ids)
        sim = len(sampled_users) - real
        return {
            "total": total,
            "distinct_user_ids_sampled": len(sampled_users),
            "real_user_ids_in_sample": real,
            "simulation_user_ids_in_sample": sim,
            "rows_without_user_id_in_sample": no_uid,
        }

    for n in raw_collections:
        out[f"raw.{n}"] = await _check(raw_db, n)
    for n in ai_collections:
        out[f"ai.{n}"] = await _check(ai_db, n)
    # Sim users are kept on the raw side.
    sim_users_total = await raw_db["simulation_users"].count_documents({})
    out["raw.simulation_users"] = {"total": sim_users_total}
    return out


def _summarise_cohort(payload: dict[str, Any]) -> CohortAudit:
    users = payload["users"]
    profiles = payload["profiles"]
    enrollments = payload["enrollments"]
    courses = payload["courses"]

    audit = CohortAudit()
    audit.real_user_count = len(users)
    audit.real_student_count = sum(1 for u in users if u.get("role_id") == 3 and u.get("is_active"))
    profile_by_uid = {str(p.get("user_id")): p for p in profiles if p.get("user_id")}
    audit.students_with_profile = sum(1 for u in users if u.get("role_id") == 3 and str(u.get("user_id")) in profile_by_uid)

    audit.students_with_class = sum(
        1 for p in profile_by_uid.values() if str(p.get("class") or "").strip()
    )
    audit.students_with_faculty = sum(
        1 for p in profile_by_uid.values() if p.get("faculty_id") is not None
    )
    audit.students_with_department = sum(
        1 for p in profile_by_uid.values() if p.get("department_id") is not None
    )

    enrolled_students = {str(e.get("student_id")) for e in enrollments if e.get("student_id")}
    audit.students_with_enrollment = len(enrolled_students)
    audit.enrollment_rows = len(enrollments)
    audit.courses_total = len(courses)
    audit.courses_with_lecturer = sum(1 for c in courses if c.get("lecturer_id"))
    course_with_enroll = {int(e["course_id"]) for e in enrollments if e.get("course_id") is not None}
    audit.courses_with_enrollment = len(course_with_enroll)
    return audit


def _print_section(title: str) -> None:
    print()
    print("=" * len(title))
    print(title)
    print("=" * len(title))


def _print_cohort(audit: CohortAudit) -> None:
    _print_section("Supabase cohort (the 'real' source of truth)")
    print(f"users.total                     = {audit.real_user_count}")
    print(f"users.role=Student & active     = {audit.real_student_count}")
    print(f"students with student_profiles  = {audit.students_with_profile}")
    print(f"   ... with class               = {audit.students_with_class}")
    print(f"   ... with faculty_id          = {audit.students_with_faculty}")
    print(f"   ... with department_id       = {audit.students_with_department}")
    print(f"students with >=1 enrollment    = {audit.students_with_enrollment}")
    print(f"courses.total                   = {audit.courses_total}")
    print(f"courses.with_lecturer           = {audit.courses_with_lecturer}")
    print(f"courses.with_enrollments        = {audit.courses_with_enrollment}")
    print(f"course_enrollments.rows         = {audit.enrollment_rows}")


def _print_mongo(stats: dict[str, dict[str, int]]) -> None:
    _print_section("MongoDB rows by user_id origin (sampled <=5,000 per collection)")
    for name, s in stats.items():
        if "real_user_ids_in_sample" in s:
            print(
                f"- {name}: total={s['total']:>7} | "
                f"distinct_uids_sampled={s['distinct_user_ids_sampled']:>4} | "
                f"real={s['real_user_ids_in_sample']:>4} | "
                f"simulation={s['simulation_user_ids_in_sample']:>4} | "
                f"no_uid_in_sample={s['rows_without_user_id_in_sample']:>3}"
            )
        else:
            print(f"- {name}: total={s['total']:>7}")


def _print_blockers(audit: CohortAudit, mongo_stats: dict[str, dict[str, int]]) -> None:
    _print_section("Readiness blockers")
    blockers: list[str] = []
    if audit.students_with_profile < audit.real_student_count:
        missing = audit.real_student_count - audit.students_with_profile
        blockers.append(
            f"{missing} student account(s) have no row in `student_profiles`. "
            "Admin should backfill profile rows before they appear in analytics."
        )
    if audit.students_with_class < audit.students_with_profile:
        missing = audit.students_with_profile - audit.students_with_class
        blockers.append(
            f"{missing} student profile(s) have no `class` value (cohort like 23k50201)."
        )
    if audit.students_with_faculty < audit.students_with_profile:
        missing = audit.students_with_profile - audit.students_with_faculty
        blockers.append(
            f"{missing} student profile(s) have no `faculty_id`. They will fall under 'Unassigned'."
        )
    if audit.students_with_department < audit.students_with_profile:
        missing = audit.students_with_profile - audit.students_with_department
        blockers.append(
            f"{missing} student profile(s) have no `department_id`. They will fall under 'Unassigned'."
        )
    if audit.students_with_enrollment < audit.real_student_count:
        missing = audit.real_student_count - audit.students_with_enrollment
        blockers.append(
            f"{missing} student(s) are not enrolled in any course; they will have no risk card."
        )
    if audit.courses_with_lecturer < audit.courses_total:
        missing = audit.courses_total - audit.courses_with_lecturer
        blockers.append(
            f"{missing} course(s) have no lecturer assigned (courses.lecturer_id IS NULL)."
        )

    sim_dominant = []
    for name, s in mongo_stats.items():
        if "simulation_user_ids_in_sample" not in s:
            continue
        sim = s.get("simulation_user_ids_in_sample", 0)
        real = s.get("real_user_ids_in_sample", 0)
        if (sim + real) and sim > real:
            sim_dominant.append(name)
    if sim_dominant:
        blockers.append(
            "Simulation user_ids dominate the following Mongo collections: "
            + ", ".join(sim_dominant)
            + ". Run `python -m ml.data.cleanup_mongo_for_real_users` to prune them."
        )

    if not blockers:
        print("READY — every real student has a profile + enrollment, and Mongo is dominated by real ids.")
        return
    for i, b in enumerate(blockers, 1):
        print(f"{i}. {b}")


async def _amain() -> int:
    payload = _read_supabase()
    audit = _summarise_cohort(payload)
    real_user_ids = {
        str(u.get("user_id")) for u in payload["users"] if u.get("user_id") and u.get("is_active")
    }
    mongo_stats = await _mongo_stats(real_user_ids)
    _print_cohort(audit)
    _print_mongo(mongo_stats)
    _print_blockers(audit, mongo_stats)
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
