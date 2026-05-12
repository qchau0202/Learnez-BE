#!/usr/bin/env python3
"""Bootstrap the primary demo account (``student1@email.com``).

``cohort.py`` is a *uniform* cohort seeder — every active student gets
one persona derived from a hash. That's fine for bulk-fill dashboards
but our **primary** demo account needs:

* fluctuative data (one persona per course, not one for the whole
  student) so the dashboards have something to talk about,
* completed foundational courses with full attendance + graded
  submissions on record,
* in-progress courses with partial progress, including one ``at_risk``
  trajectory that triggers the dropout-risk widget.

Pipeline (idempotent, safe to re-run):

1. Resolve ``student1@email.com``.
2. Enroll in five hand-picked courses (see ``ENROLLMENT_PLAN``).
3. Idempotently provision modules + assignments + questions via
   ``students.content``.
4. For each enrolment, call ``features`` / ``attendance`` /
   ``submissions`` with the per-course persona.
5. Layer ``behaviour`` (Mongo risk_scores + competency_profiles + raw
   events) on top of the just-seeded weekly features.
6. Recompute ``current_gpa`` / ``cumulative_gpa`` from the final
   submissions (10-point TDTU scale, rounded to 2 decimals).

Usage::

    cd BE
    python -m ml.data.students.student1                  # full bootstrap
    python -m ml.data.students.student1 --dry-run        # plan only
    python -m ml.data.students.student1 --weeks 18       # widen feature window

After this script, sign in as student1@email.com and open
``/student/analytics``: the risk widget should show a moderate risk
(driven by the ``at_risk`` Mobile Apps signal) and the learning-path
panel should propose the next foundational courses.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from statistics import mean
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.core.database import get_supabase  # noqa: E402

from .attendance import seed_attendance_for_student  # noqa: E402
from .behaviour import seed_behaviour_and_risk  # noqa: E402
from .content import provision_content_for_courses  # noqa: E402
from .features import PERSONAS, seed_student_features  # noqa: E402
from .submissions import seed_submissions_for_student  # noqa: E402


STUDENT_EMAIL = "student1@email.com"


# Hand-curated curriculum for the demo account. The story:
# "A 2025-cohort SE freshman who picked up programming naturally, kept
# a steady grasp on maths and OOP, is doing OK in Node.js, but is
# struggling with the new Mobile-Apps elective this semester."
#
# Per-course persona drives logins / active minutes / attendance
# distribution / score sampling.
ENROLLMENT_PLAN: tuple[tuple[str, str, str], ...] = (
    ("501031", "steady",   "Applied Calculus for IT (completed)"),
    ("501042", "thriving", "Programming Methodology (completed)"),
    ("503005", "steady",   "Object-Oriented Programming (completed)"),
    ("502070", "steady",   "Web Application Development Using NodeJS (in progress)"),
    ("503074", "at_risk",  "Mobile Apps Development (in progress)"),
)


def _svc():
    sb = get_supabase(service_role=True)
    if sb is None:
        raise SystemExit(
            "Missing Supabase service-role configuration. "
            "Export SUPABASE_SERVICE_ROLE_KEY before running."
        )
    return sb


def _resolve_student(sb, email: str) -> dict[str, Any]:
    rows = (
        sb.table("users")
        .select("user_id, full_name, email, role_id")
        .ilike("email", email)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        raise SystemExit(
            f"student1 not found ({email}). Run "
            f"`python -m ml.data.students.provision` first to create the demo cohort."
        )
    user = rows[0]
    if user.get("role_id") != 3:
        raise SystemExit(
            f"{email} is not a student (role_id={user.get('role_id')}). "
            "Refusing to seed analytics for non-student accounts."
        )
    return user


def _resolve_courses_by_code(sb, codes: list[str]) -> dict[str, dict[str, Any]]:
    """Return ``{course_code: row}`` — full row needed by the per-course seeders."""
    rows = (
        sb.table("courses")
        .select(
            "id, course_code, title, course_session, course_session_date, "
            "course_session_duration, course_start_date, course_end_date, "
            "course_occurences, lecturer_id, is_complete, academic_year, semester"
        )
        .in_("course_code", codes)
        .execute()
        .data
        or []
    )
    return {str(r["course_code"]): r for r in rows if r.get("course_code")}


def _ensure_enrollment(sb, *, user_id: str, course_id: int, dry_run: bool) -> bool:
    """Upsert ``(course_id, student_id)``. Returns ``True`` if the row was created."""
    existing = (
        sb.table("course_enrollments")
        .select("course_id, student_id")
        .eq("course_id", course_id)
        .eq("student_id", user_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing:
        return False
    if dry_run:
        return True
    sb.table("course_enrollments").upsert(
        {"course_id": course_id, "student_id": user_id},
        on_conflict="course_id,student_id",
    ).execute()
    return True


# ---------- GPA recomputation ---------- #


def _course_average(sb, *, user_id: str, course_id: int) -> float | None:
    """Average final_score for one student on one course (10-point scale).

    Only finalised submissions count. Returns ``None`` if no grades
    exist yet.
    """
    rows = (
        sb.table("assignment_submissions")
        .select("final_score, assignment_id, assignments!inner(module_id, modules!inner(course_id))")
        .eq("student_id", user_id)
        .eq("assignments.modules.course_id", course_id)
        .not_.is_("final_score", None)
        .execute()
        .data
        or []
    )
    scores = [float(r["final_score"]) for r in rows if r.get("final_score") is not None]
    if not scores:
        return None
    return sum(scores) / len(scores)


def _recompute_gpa(
    sb, *, user_id: str, courses_by_code: dict[str, dict[str, Any]],
) -> tuple[float | None, float | None]:
    """Return ``(current_gpa, cumulative_gpa)`` (10-point scale, 2-decimal).

    ``cumulative_gpa`` = mean across courses with ``is_complete=true``.
    ``current_gpa`` = mean across active courses, falling back to
    cumulative when there's no live data yet.
    """
    completed_scores: list[float] = []
    active_scores: list[float] = []
    for _code, course in courses_by_code.items():
        course_id = int(course["id"])
        avg = _course_average(sb, user_id=user_id, course_id=course_id)
        if avg is None:
            continue
        if course.get("is_complete"):
            completed_scores.append(avg)
        else:
            active_scores.append(avg)

    cumulative = round(mean(completed_scores), 2) if completed_scores else None
    current = round(mean(active_scores), 2) if active_scores else cumulative
    return current, cumulative


def _update_profile_gpa(
    sb, *,
    user_id: str,
    current_gpa: float | None,
    cumulative_gpa: float | None,
    dry_run: bool,
) -> None:
    if cumulative_gpa is None and current_gpa is None:
        return
    payload: dict[str, Any] = {}
    if current_gpa is not None:
        payload["current_gpa"] = current_gpa
    if cumulative_gpa is not None:
        payload["cumulative_gpa"] = cumulative_gpa
    if not payload:
        return
    if dry_run:
        return
    sb.table("student_profiles").update(payload).eq("user_id", user_id).execute()


# ---------- Orchestration ---------- #


async def _seed_one_course(
    sb, *,
    user_id: str,
    course: dict[str, Any],
    persona: str,
    weeks: int,
    dry_run: bool,
    log_prefix: str,
) -> dict[str, int]:
    """Run features + attendance + submissions for a single course/persona."""
    counts = {"features": 0, "attendance": 0, "submissions": 0}
    if dry_run:
        print(f"{log_prefix}dry-run: would seed persona={persona} for course id={course['id']}")
        return counts

    counts["features"] = await seed_student_features(
        user_id=user_id,
        courses=[course],
        persona_name=persona,
        weeks=weeks,
        ignore_course_window=False,
        log_prefix="",
    )
    counts["attendance"] = await asyncio.to_thread(
        seed_attendance_for_student,
        sb,
        user_id=user_id,
        courses=[course],
        persona_name=persona,
        log_prefix="",
    )
    counts["submissions"] = await asyncio.to_thread(
        seed_submissions_for_student,
        sb,
        user_id=user_id,
        courses=[course],
        persona_name=persona,
        log_prefix="",
    )
    return counts


async def _run(args: argparse.Namespace) -> int:
    sb = _svc()
    user = _resolve_student(sb, args.email)
    user_id = str(user["user_id"])

    plan_codes = [code for code, _persona, _label in ENROLLMENT_PLAN]
    courses_by_code = _resolve_courses_by_code(sb, plan_codes)

    missing = [c for c in plan_codes if c not in courses_by_code]
    if missing:
        raise SystemExit(
            f"The following demo courses are missing from `public.courses`: {missing}. "
            f"Run `python -m ml.data.curriculum.seed --sync-only --refresh-schedule` "
            f"to publish them first."
        )

    print("=" * 72)
    print(f"Bootstrapping {user.get('full_name') or args.email} ({user_id})")
    print(f"  Courses to enroll: {len(ENROLLMENT_PLAN)}")
    for code, persona, label in ENROLLMENT_PLAN:
        complete = "✓" if courses_by_code[code].get("is_complete") else "·"
        print(f"    [{complete}] {code}  persona={persona:<8}  {label}")
    print(f"  Weeks of features    : {args.weeks}")
    print(f"  Dry-run              : {args.dry_run}")
    print("=" * 72)

    # Modules + assignments + questions must exist BEFORE the per-course
    # submission seeder runs; otherwise it has nothing to attach to and
    # GPA stays empty. The call is idempotent.
    if args.skip_content:
        print("Course content provisioning skipped (--skip-content).")
    else:
        print("Course content (modules + assignments + questions):")
        try:
            content_counts = await asyncio.to_thread(
                provision_content_for_courses,
                sb,
                courses=list(courses_by_code.values()),
                dry_run=args.dry_run,
            )
            print(
                f"    modules={content_counts['modules']:>3}  "
                f"assignments={content_counts['assignments']:>3}  "
                f"questions={content_counts['questions']:>3}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"    ERROR provisioning course content: {exc!r}")
        print("-" * 72)

    new_enrollments = 0
    totals = {"features": 0, "attendance": 0, "submissions": 0}
    for idx, (code, persona, label) in enumerate(ENROLLMENT_PLAN, start=1):
        course = courses_by_code[code]
        course_id = int(course["id"])
        prefix = f"[{idx}/{len(ENROLLMENT_PLAN)}] {code} "

        created = _ensure_enrollment(
            sb, user_id=user_id, course_id=course_id, dry_run=args.dry_run
        )
        if created:
            new_enrollments += 1
        action = "enroll+seed" if created else "seed only "
        print(f"{prefix}{action} ({label}, persona={persona})")

        try:
            counts = await _seed_one_course(
                sb,
                user_id=user_id,
                course=course,
                persona=persona,
                weeks=args.weeks,
                dry_run=args.dry_run,
                log_prefix="    ",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"    ERROR while seeding {code}: {exc!r}")
            continue

        for key, value in counts.items():
            totals[key] += value
        print(
            f"    features={counts['features']:>3}  "
            f"attendance={counts['attendance']:>3}  "
            f"submissions={counts['submissions']:>3}"
        )

    print("-" * 72)
    print(
        f"enrolled (new) : {new_enrollments}/{len(ENROLLMENT_PLAN)}  "
        f"features={totals['features']} attendance={totals['attendance']} "
        f"submissions={totals['submissions']}"
    )

    # Behaviour + risk side-tables — best-effort: any failure logs and
    # continues so we never fail the whole bootstrap because Mongo is
    # unreachable.
    if args.skip_behaviour:
        print("Behaviour + risk seeding skipped (--skip-behaviour).")
    else:
        print("-" * 72)
        print("Behaviour + risk side-tables:")
        try:
            beh_counts = await seed_behaviour_and_risk(
                user_id=user_id,
                since_weeks=args.weeks,
                skip_events=args.skip_events,
                dry_run=args.dry_run,
            )
            total_beh = sum(beh_counts.values())
            beh_detail = "  ".join(f"{k}={v}" for k, v in beh_counts.items())
            print(f"  total docs written : {total_beh}")
            print(f"  {beh_detail}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR: behaviour seeder failed: {exc!r}")
            print("  Supabase data is intact; re-run with --skip-behaviour to skip this phase.")

    current_gpa, cumulative_gpa = _recompute_gpa(
        sb, user_id=user_id, courses_by_code=courses_by_code
    )
    _update_profile_gpa(
        sb,
        user_id=user_id,
        current_gpa=current_gpa,
        cumulative_gpa=cumulative_gpa,
        dry_run=args.dry_run,
    )

    print(
        f"GPA            : current={current_gpa}  cumulative={cumulative_gpa} "
        f"(10-point TDTU scale, mean of final_score per course)"
    )
    print("=" * 72)
    if args.dry_run:
        print("dry-run: nothing was written.")
    else:
        print(
            "Done. Next steps:\n"
            "  1. (optional) refresh weekly snapshots: \n"
            "       python -m ml.training.sample_dropout_predictions\n"
            "  2. Sign in as student1@email.com and open the analytics + learning path tabs."
        )
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bootstrap analytics + learning-path foundation for student1@email.com.",
    )
    p.add_argument("--email", default=STUDENT_EMAIL,
                   help=f"Demo account email (default {STUDENT_EMAIL!r}).")
    p.add_argument("--weeks", type=int, default=16,
                   help="Weekly feature look-back window (default 16).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan, write nothing.")
    p.add_argument("--skip-content", action="store_true",
                   help="Skip module + assignment + question provisioning.")
    p.add_argument("--skip-behaviour", action="store_true",
                   help="Skip the Mongo behaviour + risk side-tables.")
    p.add_argument("--skip-events", action="store_true",
                   help="Within --skip-behaviour, still write risk_scores + competency but skip elearning_raw.*")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
