"""Provision modules + assignments + questions for the demo courses.

The other ``students.*`` seeders create student-side records (enrolments,
features, submissions). This script idempotently creates the academic
content they reference so the demo actually has GPA and grading flows.

Per course we create 8 modules. Each module gets one assignment whose
type alternates so every grading surface (auto-graded MCQ, mixed,
essay, manual) is exercised. Each assignment has ``total_score=10.0``
(TDTU 10-point scale). Due dates are spread evenly across the course
window so completed courses have all 8 past-due and in-progress ones
have the first few.

Usage::

    cd BE
    python -m ml.data.students.content                    # student1's 5 curated courses
    python -m ml.data.students.content --all-enrolled     # every course they're enrolled in
    python -m ml.data.students.content --email student2@email.com
    python -m ml.data.students.content --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.core.database import get_supabase  # noqa: E402

# NOTE: ``student1`` imports this module at runtime; the CLI lazily
# imports ENROLLMENT_PLAN to avoid a circular import.


# Each entry: module + the one assignment that lives inside it.
# ``due_pct`` controls when in the course window it's due (0.0 = day 1, 1.0 = last day).
MODULE_PLAN: tuple[dict[str, Any], ...] = (
    {
        "title_suffix": "Foundations",
        "description": "Core concepts, vocabulary, and warm-up exercises.",
        "assignment_title_suffix": "Foundations Quiz",
        "kind": "mcq", "due_pct": 0.10, "mcq_count": 8,
    },
    {
        "title_suffix": "Building Blocks",
        "description": "Drills on the primitives introduced in the first module.",
        "assignment_title_suffix": "Building Blocks Quiz",
        "kind": "mcq", "due_pct": 0.22, "mcq_count": 6,
    },
    {
        "title_suffix": "Applied Workshop",
        "description": "Guided problems that combine the first two weeks' ideas.",
        "assignment_title_suffix": "Workshop Worksheet",
        "kind": "mixed", "due_pct": 0.34, "mcq_count": 4, "essay_prompts": 1,
    },
    {
        "title_suffix": "Deep Dive Theory",
        "description": "Conceptual writing prompts that require synthesis and short proofs.",
        "assignment_title_suffix": "Concept Essay",
        "kind": "essay", "due_pct": 0.46, "essay_prompts": 2,
    },
    {
        "title_suffix": "Practical Lab",
        "description": "Hands-on lab graded against a rubric by the lecturer.",
        "assignment_title_suffix": "Lab Report",
        "kind": "manual", "due_pct": 0.58,
    },
    {
        "title_suffix": "Midterm Assessment",
        "description": "Mixed checkpoint covering the first half of the syllabus.",
        "assignment_title_suffix": "Midterm Exam",
        "kind": "mixed", "due_pct": 0.70, "mcq_count": 6, "essay_prompts": 1,
    },
    {
        "title_suffix": "Advanced Topics",
        "description": "Selected advanced topics with applied scenarios.",
        "assignment_title_suffix": "Advanced Topics Quiz",
        "kind": "mcq", "due_pct": 0.82, "mcq_count": 8,
    },
    {
        "title_suffix": "Capstone Project",
        "description": "Final integrative work — submitted as a single artefact.",
        "assignment_title_suffix": "Capstone Submission",
        "kind": "manual", "due_pct": 0.95,
    },
)


# Generic MCQ bank — the dropout-risk pipeline cares about score
# distribution, not question correctness.
_MCQ_BANK: tuple[dict[str, Any], ...] = (
    {
        "content": "Which of these best describes a well-defined learning objective?",
        "options": [
            ("A", "A vague aspiration without measurable outcomes."),
            ("B", "An observable behaviour with a measurable target and context."),
            ("C", "A list of every topic in the textbook."),
            ("D", "An exam date."),
        ],
        "correct": "B",
    },
    {
        "content": "When prioritising tasks under time pressure, the most useful question is:",
        "options": [
            ("A", "Which task is the easiest?"),
            ("B", "Which task has the highest impact for the effort required?"),
            ("C", "Which task is the most fun?"),
            ("D", "Which task is the longest?"),
        ],
        "correct": "B",
    },
    {
        "content": "Reading actively differs from passive reading because:",
        "options": [
            ("A", "It is always faster."),
            ("B", "It requires the reader to question and connect ideas while reading."),
            ("C", "It involves rewriting the text word-for-word."),
            ("D", "It happens only on paper, never on screens."),
        ],
        "correct": "B",
    },
    {
        "content": "Which is the strongest indicator of mastery in a topic?",
        "options": [
            ("A", "Memorising the textbook chapter."),
            ("B", "Being able to teach the concept to someone unfamiliar with it."),
            ("C", "Reading the slides twice."),
            ("D", "Highlighting paragraphs."),
        ],
        "correct": "B",
    },
    {
        "content": "Effective feedback is most useful when it is:",
        "options": [
            ("A", "Specific, timely, and actionable."),
            ("B", "Vague but encouraging."),
            ("C", "Delivered weeks after the work was done."),
            ("D", "Limited to a single overall grade."),
        ],
        "correct": "A",
    },
    {
        "content": "Spaced repetition works because:",
        "options": [
            ("A", "Reviewing topics at increasing intervals strengthens long-term recall."),
            ("B", "It minimises the number of practice sessions."),
            ("C", "It only works for vocabulary."),
            ("D", "It removes the need to take notes."),
        ],
        "correct": "A",
    },
    {
        "content": "A clear problem statement should typically include:",
        "options": [
            ("A", "Only the desired solution."),
            ("B", "The context, the gap, and the impact of solving it."),
            ("C", "Only the deadline."),
            ("D", "The author's bibliography."),
        ],
        "correct": "B",
    },
    {
        "content": "Which collaboration practice helps a team make decisions efficiently?",
        "options": [
            ("A", "Skipping retrospectives entirely."),
            ("B", "Documenting trade-offs and assumptions explicitly."),
            ("C", "Voting silently without discussion."),
            ("D", "Always deferring to the loudest team member."),
        ],
        "correct": "B",
    },
)


_ESSAY_PROMPTS: tuple[str, ...] = (
    "Summarise the three most important ideas from this module and connect them to a real-world example.",
    "Pick a concept that surprised you and explain how it changes the way you would approach a similar problem in the future.",
    "Describe a trade-off you encountered while studying this module's material. What would you do differently next time?",
    "Outline a small project that would let you practice the techniques from this module. What signals would tell you that you've improved?",
    "Compare two approaches you learned this week. When would you pick one over the other, and why?",
)


def _svc():
    sb = get_supabase(service_role=True)
    if sb is None:
        raise SystemExit(
            "Missing Supabase service-role configuration. "
            "Export SUPABASE_SERVICE_ROLE_KEY before running."
        )
    return sb


def _seeded_rng(*parts: Any) -> random.Random:
    import hashlib

    digest = hashlib.sha1(":".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _due_datetime_for(
    *, course: dict[str, Any], pct: float, rng: random.Random,
) -> datetime:
    """Pick a due-datetime ``pct`` of the way through the course window."""
    start = _parse_date(course.get("course_start_date"))
    end = _parse_date(course.get("course_end_date"))
    now = datetime.now(timezone.utc)
    if start and end and end > start:
        span_days = (end - start).days
        offset_days = max(1, int(span_days * pct))
        target_date = start + timedelta(days=offset_days)
    else:
        target_date = (now + timedelta(weeks=2)).date()
    return datetime.combine(
        target_date, time(23, 59 - rng.randint(0, 25)), tzinfo=timezone.utc,
    )


def _resolve_existing_modules(sb, course_id: int) -> dict[str, dict[str, Any]]:
    rows = (
        sb.table("modules")
        .select("id, title, description, course_id")
        .eq("course_id", course_id)
        .execute()
        .data
        or []
    )
    return {(r.get("title") or "").strip().lower(): r for r in rows}


def _ensure_module(
    sb, *,
    course_id: int,
    title: str,
    description: str,
    existing_index: dict[str, dict[str, Any]],
    dry_run: bool,
) -> tuple[dict[str, Any] | None, bool]:
    """Create the module if missing. Returns ``(row, created)``."""
    key = title.strip().lower()
    if key in existing_index:
        return existing_index[key], False
    if dry_run:
        return (
            {"id": -1, "title": title, "description": description, "course_id": course_id},
            True,
        )
    inserted = (
        sb.table("modules")
        .insert({"course_id": course_id, "title": title, "description": description})
        .execute()
        .data
        or []
    )
    if not inserted:
        return None, False
    row = inserted[0]
    existing_index[key] = row
    return row, True


def _resolve_existing_assignments(sb, module_id: int) -> dict[str, dict[str, Any]]:
    rows = (
        sb.table("assignments")
        .select("id, title, module_id, total_score")
        .eq("module_id", module_id)
        .execute()
        .data
        or []
    )
    return {(r.get("title") or "").strip().lower(): r for r in rows}


def _build_questions(*, plan: dict[str, Any], rng: random.Random) -> list[dict[str, Any]]:
    """Return ``assignment_questions`` rows (without ``assignment_id``)."""
    out: list[dict[str, Any]] = []
    kind = plan["kind"]
    order = 0
    if kind in {"mcq", "mixed"}:
        mcq_count = int(plan.get("mcq_count") or 5)
        pool = list(_MCQ_BANK)
        rng.shuffle(pool)
        for q in pool[:mcq_count]:
            metadata = {
                "options": [{"id": opt_id, "text": opt_text} for opt_id, opt_text in q["options"]],
                "correct_option_ids": [q["correct"]],
                "allow_multiple": False,
            }
            out.append(
                {"type": "mcq", "content": q["content"], "order_index": order, "metadata": metadata}
            )
            order += 1
    if kind in {"essay", "mixed"}:
        essay_prompts = int(plan.get("essay_prompts") or 1)
        prompts = list(_ESSAY_PROMPTS)
        rng.shuffle(prompts)
        for prompt in prompts[:essay_prompts]:
            out.append(
                {
                    "type": "essay",
                    "content": prompt,
                    "order_index": order,
                    "metadata": {"min_words": 80, "max_words": 300},
                }
            )
            order += 1
    # Manual assignments produce zero questions on purpose: the backend
    # infers ``mode='manual'`` from the absence of question rows, and the
    # ``type`` CHECK constraint rejects anything outside {mcq, essay}.
    return out


def _ensure_assignment(
    sb, *,
    module_id: int,
    course: dict[str, Any],
    plan: dict[str, Any],
    lecturer_id: str | None,
    existing_index: dict[str, dict[str, Any]],
    dry_run: bool,
    rng: random.Random,
) -> tuple[dict[str, Any] | None, int, bool]:
    """Create one assignment + questions if missing.

    Returns ``(row, questions_inserted, assignment_created)``. If the
    assignment exists we don't touch its questions (re-running would
    otherwise append duplicates).
    """
    course_code = str(course.get("course_code") or course.get("id"))
    title = f"{course_code} · {plan['assignment_title_suffix']}"
    key = title.strip().lower()
    if key in existing_index:
        return existing_index[key], 0, False

    due_dt = _due_datetime_for(course=course, pct=float(plan["due_pct"]), rng=rng)
    hard_due_dt = due_dt + timedelta(days=2)

    description = (
        f"{course.get('title', course_code)} — {plan['title_suffix']} assessment. "
        "Auto-generated for the demo cohort."
    )

    if dry_run:
        return ({"id": -1, "title": title, "module_id": module_id, "total_score": 10.0}, 0, True)

    payload: dict[str, Any] = {
        "module_id": module_id,
        "title": title,
        "description": description,
        "due_date": due_dt.isoformat(),
        "hard_due_date": hard_due_dt.isoformat(),
        "total_score": 10.0,
        "is_graded": True,
        "duration_enabled": plan["kind"] in {"mcq", "mixed"},
    }
    if plan["kind"] == "mcq":
        payload["duration"] = 30
    elif plan["kind"] == "mixed":
        payload["duration"] = 60
    if lecturer_id:
        payload["uploaded_by"] = lecturer_id

    inserted = sb.table("assignments").insert(payload).execute().data or []
    if not inserted:
        return None, 0, False
    row = inserted[0]
    existing_index[key] = row

    questions = _build_questions(plan=plan, rng=rng)
    if questions:
        question_payloads = [{**q, "assignment_id": int(row["id"])} for q in questions]
        sb.table("assignment_questions").insert(question_payloads).execute()
    return row, len(questions), True


def _resolve_courses_for_codes(sb, codes: list[str]) -> list[dict[str, Any]]:
    if not codes:
        return []
    rows = (
        sb.table("courses")
        .select(
            "id, course_code, title, lecturer_id, course_start_date, "
            "course_end_date, semester, academic_year, is_complete"
        )
        .in_("course_code", codes)
        .execute()
        .data
        or []
    )
    return rows


def _resolve_courses_for_student(sb, email: str) -> list[dict[str, Any]]:
    user_rows = (
        sb.table("users").select("user_id").ilike("email", email).limit(1).execute().data or []
    )
    if not user_rows:
        raise SystemExit(f"User {email} not found.")
    user_id = str(user_rows[0]["user_id"])
    enrol_rows = (
        sb.table("course_enrollments")
        .select("course_id")
        .eq("student_id", user_id)
        .execute()
        .data
        or []
    )
    ids = sorted({int(r["course_id"]) for r in enrol_rows if r.get("course_id") is not None})
    if not ids:
        return []
    out: list[dict[str, Any]] = []
    for i in range(0, len(ids), 200):
        batch = ids[i : i + 200]
        rows = (
            sb.table("courses")
            .select(
                "id, course_code, title, lecturer_id, course_start_date, "
                "course_end_date, semester, academic_year, is_complete"
            )
            .in_("id", batch)
            .execute()
            .data
            or []
        )
        out.extend(rows)
    return out


def provision_content_for_courses(
    sb, *, courses: list[dict[str, Any]], dry_run: bool = False,
) -> dict[str, int]:
    """Idempotently provision modules + assignments for each course."""
    counts = {"modules": 0, "assignments": 0, "questions": 0}
    for course in courses:
        course_id = int(course["id"])
        course_code = course.get("course_code") or str(course_id)
        lecturer_id = course.get("lecturer_id")
        if not lecturer_id:
            print(
                f"  [warn] {course_code}: course has no lecturer_id — "
                "assignments will not have an owner."
            )

        modules_index = _resolve_existing_modules(sb, course_id)
        for plan in MODULE_PLAN:
            module_title = f"Module · {plan['title_suffix']}"
            module, module_created = _ensure_module(
                sb, course_id=course_id, title=module_title,
                description=plan["description"], existing_index=modules_index, dry_run=dry_run,
            )
            if module is None:
                continue
            if module_created:
                counts["modules"] += 1

            module_id = int(module["id"])
            if module_id == -1:
                # dry-run path — count the assignment for accurate output.
                counts["assignments"] += 1
                continue

            assignments_index = _resolve_existing_assignments(sb, module_id)
            rng = _seeded_rng("course-content", course_id, plan["title_suffix"])
            assignment, q_count, assignment_created = _ensure_assignment(
                sb, module_id=module_id, course=course, plan=plan,
                lecturer_id=lecturer_id, existing_index=assignments_index,
                dry_run=dry_run, rng=rng,
            )
            if assignment_created:
                counts["assignments"] += 1
                counts["questions"] += q_count
    return counts


def _print_plan_summary(courses: list[dict[str, Any]]) -> None:
    print("=" * 72)
    print(f"Provisioning content for {len(courses)} course(s):")
    for c in courses:
        complete = "✓" if c.get("is_complete") else "·"
        start = c.get("course_start_date") or "no-start"
        end = c.get("course_end_date") or "no-end"
        lecturer = c.get("lecturer_id") or "no-lecturer"
        print(
            f"  [{complete}] {c.get('course_code'):<10} {start} → {end}  "
            f"lecturer={lecturer[:8]}…  {c.get('title')}"
        )
    print(f"  per course: {len(MODULE_PLAN)} modules · {len(MODULE_PLAN)} assignments")
    print("=" * 72)


async def _run(args: argparse.Namespace) -> int:
    sb = _svc()
    # Lazy import — keeps ``student1`` ↔ ``content`` DAG acyclic.
    from .student1 import ENROLLMENT_PLAN  # noqa: WPS433

    if args.all_enrolled:
        courses = _resolve_courses_for_student(sb, args.email)
        if not courses:
            raise SystemExit(f"No enrolments for {args.email}; nothing to do.")
    else:
        plan_codes = [code for code, _persona, _label in ENROLLMENT_PLAN]
        courses = _resolve_courses_for_codes(sb, plan_codes)
        if not courses:
            raise SystemExit(
                "No courses in scope. Re-run with --all-enrolled or check that the curriculum "
                "has been synced via `python -m ml.data.curriculum.seed`."
            )

    _print_plan_summary(courses)
    counts = provision_content_for_courses(sb, courses=courses, dry_run=args.dry_run)

    print("-" * 72)
    print(
        f"modules    : {counts['modules']}\n"
        f"assignments: {counts['assignments']}\n"
        f"questions  : {counts['questions']}"
    )
    if args.dry_run:
        print("dry-run: nothing was actually persisted.")
    else:
        print(
            "Done. Next step: run `python -m ml.data.students.student1` so the "
            "student's submissions reference the new assignments and GPA is recomputed."
        )
    print("=" * 72)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Provision modules + assignments + questions for the demo courses. "
            "Idempotent — safe to re-run."
        ),
    )
    p.add_argument("--email", default="student1@email.com",
                   help="Email scoped against when --all-enrolled is set.")
    p.add_argument("--all-enrolled", action="store_true",
                   help="Provision for every course the student is enrolled in.")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
