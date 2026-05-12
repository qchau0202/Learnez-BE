#!/usr/bin/env python3
"""Read-only diagnostic for a demo student's dataset.

Prints a wide table of what currently exists in Supabase and MongoDB
so we can tell at a glance which pieces of the seeding pipeline have
actually run. Safe to use in production-like environments — never
writes anywhere.

Usage::

    cd BE
    python -m ml.data.students.diagnose
    python -m ml.data.students.diagnose --email student2@email.com
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.core.database import get_supabase  # noqa: E402

DEFAULT_EMAIL = "student1@email.com"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(value: Any) -> str:
    """Truncate long strings for tabular output."""
    if value is None:
        return "—"
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "…"


def _diagnose_supabase(email: str) -> dict[str, Any]:
    sb = get_supabase(service_role=True)
    if not sb:
        return {"error": "Supabase service-role client not configured."}

    user_rows = (
        sb.table("users").select("user_id, full_name, email, role_id")
        .ilike("email", email).limit(1).execute().data
    )
    if not user_rows:
        return {"error": f"User {email!r} not found."}
    user = user_rows[0]
    user_id = str(user["user_id"])

    profile_rows = (
        sb.table("student_profiles")
        .select("student_id, current_gpa, cumulative_gpa, major, enrolled_year")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
        .data
    )
    profile = profile_rows[0] if profile_rows else None

    enrol_rows = (
        sb.table("course_enrollments")
        .select("course_id")
        .eq("student_id", user_id)
        .execute()
        .data
        or []
    )
    course_ids = sorted({int(r["course_id"]) for r in enrol_rows if r.get("course_id") is not None})

    course_rows = (
        sb.table("courses")
        .select(
            "id, course_code, title, lecturer_id, course_start_date, course_end_date, is_complete, "
            "academic_year, semester"
        )
        .in_("id", course_ids or [-1])
        .execute()
        .data
        or []
    )
    courses_by_id = {int(c["id"]): c for c in course_rows}

    module_rows = (
        sb.table("modules").select("id, course_id, title")
        .in_("course_id", course_ids or [-1]).execute().data
        or []
    )
    modules_per_course: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for m in module_rows:
        modules_per_course[int(m["course_id"])].append(m)

    module_ids = [int(m["id"]) for m in module_rows]
    assignment_rows: list[dict[str, Any]] = []
    if module_ids:
        for i in range(0, len(module_ids), 200):
            batch = module_ids[i : i + 200]
            rows = (
                sb.table("assignments")
                .select("id, module_id, title, due_date, total_score, is_graded")
                .in_("module_id", batch)
                .execute()
                .data
                or []
            )
            assignment_rows.extend(rows)
    module_to_course = {int(m["id"]): int(m["course_id"]) for m in module_rows}
    assignments_per_course: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for a in assignment_rows:
        cid = module_to_course.get(int(a["module_id"] or -1))
        if cid is not None:
            a["course_id"] = cid
            assignments_per_course[cid].append(a)

    submission_rows = (
        sb.table("assignment_submissions")
        .select("id, assignment_id, final_score, is_corrected, submitted_at")
        .eq("student_id", user_id)
        .execute()
        .data
        or []
    )
    aid_to_course = {int(a["id"]): int(a["course_id"]) for a in assignment_rows}
    submissions_per_course: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for s in submission_rows:
        cid = aid_to_course.get(int(s["assignment_id"] or -1))
        if cid is not None:
            submissions_per_course[cid].append(s)

    attendance_rows = (
        sb.table("course_attendance")
        .select("course_id, status")
        .eq("student_id", user_id)
        .execute()
        .data
        or []
    )
    attendance_per_course: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in attendance_rows:
        cid = int(r["course_id"]) if r.get("course_id") is not None else None
        if cid is None:
            continue
        status = (r.get("status") or "unknown").lower()
        attendance_per_course[cid][status] += 1

    # ``student_weekly_features`` is a Mongo collection; backfill in ``_diagnose_mongo``.
    features_per_course: dict[int, int] = defaultdict(int)

    return {
        "user": user,
        "user_id": user_id,
        "profile": profile,
        "courses": courses_by_id,
        "course_ids": course_ids,
        "modules_per_course": modules_per_course,
        "assignments_per_course": assignments_per_course,
        "submissions_per_course": submissions_per_course,
        "attendance_per_course": attendance_per_course,
        "features_per_course": features_per_course,
    }


async def _diagnose_mongo(user_id: str) -> dict[str, Any]:
    try:
        from app.core.database import get_mongo_ai_db, get_mongo_raw_db
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Mongo client unavailable: {exc!r}"}

    ai_db = get_mongo_ai_db()
    raw_db = get_mongo_raw_db()

    out: dict[str, Any] = {}
    features_per_course: dict[int, int] = defaultdict(int)
    if ai_db is not None:
        out["risk_scores"] = await ai_db["risk_scores"].count_documents({"user_id": user_id})
        out["competency_profiles"] = await ai_db["competency_profiles"].count_documents(
            {"user_id": user_id}
        )
        out["learning_paths"] = await ai_db["learning_paths"].count_documents({"user_id": user_id})
        out["student_path_picks"] = await ai_db["student_path_picks"].count_documents(
            {"user_id": user_id}
        )
        out["agent_runs_total"] = await ai_db["agent_runs"].count_documents({"user_id": user_id})
        out["agent_runs_active"] = await ai_db["agent_runs"].count_documents(
            {"user_id": user_id, "status": {"$ne": "deleted"}}
        )
        out["learning_path_intake_sessions"] = await ai_db[
            "learning_path_intake_sessions"
        ].count_documents({"user_id": user_id})
        out["student_weekly_features"] = await ai_db[
            "student_weekly_features"
        ].count_documents({"user_id": user_id})
        async for doc in ai_db["student_weekly_features"].find(
            {"user_id": user_id}, {"course_id": 1}
        ):
            cid = doc.get("course_id")
            if cid is not None:
                features_per_course[int(cid)] += 1
    else:
        out["ai_db"] = "AI DB connection not available."
    out["_features_per_course"] = features_per_course

    if raw_db is not None:
        out["activity_events"] = await raw_db["activity_events"].count_documents({"user_id": user_id})
        out["content_events"] = await raw_db["content_events"].count_documents({"user_id": user_id})
        out["attendance_events"] = await raw_db["attendance_events"].count_documents(
            {"user_id": user_id}
        )
        out["assessment_events"] = await raw_db["assessment_events"].count_documents(
            {"user_id": user_id}
        )
        out["chat_events"] = await raw_db["chat_events"].count_documents({"user_id": user_id})
        out["ai_action_events"] = await raw_db["ai_action_events"].count_documents(
            {"user_id": user_id}
        )
    else:
        out["raw_db"] = "Raw DB connection not available."

    return out


def _print_supabase_report(report: dict[str, Any]) -> None:
    if "error" in report:
        print(f"[supabase] ERROR: {report['error']}")
        return
    user = report["user"]
    profile = report["profile"] or {}
    print("=" * 90)
    print(f"User         : {user.get('full_name')!r}  <{user.get('email')}>  id={report['user_id'][:8]}…")
    print(
        f"Profile      : student_id={profile.get('student_id')}  "
        f"current_gpa={profile.get('current_gpa')}  cumulative_gpa={profile.get('cumulative_gpa')}  "
        f"major={profile.get('major')}"
    )
    print(f"Enrolments   : {len(report['course_ids'])} courses")
    if not report["course_ids"]:
        return

    header = (
        f"{'code':<8} {'title':<46} {'mods':>4} {'asgn':>4} {'past':>4} "
        f"{'subm':>4} {'grade':>5} {'feat':>4} {'att':>10} {'complete':>9}"
    )
    print("-" * len(header))
    print(header)
    print("-" * len(header))
    now = _now()
    grand_total = {"modules": 0, "assignments": 0, "past": 0, "submissions": 0, "graded": 0}
    sorted_ids = sorted(
        report["course_ids"],
        key=lambda cid: (report["courses"].get(cid) or {}).get("course_code") or "",
    )
    for cid in sorted_ids:
        course = report["courses"].get(cid) or {}
        modules = report["modules_per_course"].get(cid, [])
        assignments = report["assignments_per_course"].get(cid, [])
        submissions = report["submissions_per_course"].get(cid, [])
        attendance = report["attendance_per_course"].get(cid, {})
        features = report["features_per_course"].get(cid, 0)

        past_assignments = 0
        for a in assignments:
            due = a.get("due_date")
            if not due:
                continue
            try:
                dt = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt <= now:
                    past_assignments += 1
            except ValueError:
                continue

        graded = sum(1 for s in submissions if s.get("is_corrected") and s.get("final_score") is not None)
        att_present = attendance.get("present", 0) + attendance.get("late", 0)
        att_total = sum(attendance.values())
        att_str = f"{att_present}/{att_total}" if att_total else "—"

        print(
            f"{course.get('course_code') or str(cid):<8} "
            f"{_fmt(course.get('title')):<46} "
            f"{len(modules):>4} "
            f"{len(assignments):>4} "
            f"{past_assignments:>4} "
            f"{len(submissions):>4} "
            f"{graded:>5} "
            f"{features:>4} "
            f"{att_str:>10} "
            f"{'yes' if course.get('is_complete') else 'no':>9}"
        )
        grand_total["modules"] += len(modules)
        grand_total["assignments"] += len(assignments)
        grand_total["past"] += past_assignments
        grand_total["submissions"] += len(submissions)
        grand_total["graded"] += graded
    print("-" * len(header))
    print(
        f"{'TOTAL':<55} {grand_total['modules']:>4} {grand_total['assignments']:>4} "
        f"{grand_total['past']:>4} {grand_total['submissions']:>4} {grand_total['graded']:>5}"
    )
    print("=" * 90)


def _print_mongo_report(report: dict[str, Any]) -> None:
    print("MongoDB collections (counts for this user)")
    print("-" * 60)
    if "error" in report:
        print(f"[mongo] ERROR: {report['error']}")
        return
    for key in sorted(report.keys()):
        value = report[key]
        print(f"  {key:<32} {value}")
    print("=" * 90)


async def _run(args: argparse.Namespace) -> int:
    report = _diagnose_supabase(args.email)
    mongo_report: dict[str, Any] = {}
    if "user_id" in report:
        mongo_report = await _diagnose_mongo(report["user_id"])
        if "_features_per_course" in mongo_report:
            report["features_per_course"] = mongo_report["_features_per_course"]
    _print_supabase_report(report)
    if mongo_report:
        _print_mongo_report(
            {k: v for k, v in mongo_report.items() if not k.startswith("_")}
        )
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Print a per-course / per-collection data audit for a student.")
    p.add_argument("--email", default=DEFAULT_EMAIL)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
