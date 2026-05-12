"""End-to-end cohort seeder.

Gives every active student a believable behavioural fingerprint so
lecturer/admin analytics views (at-risk lists, grade distributions) are
populated without depending on real activity.

Pipeline:

1. Pull every active ``role=student`` user + enrolments.
2. Hash each student into one of ``{thriving, steady, at_risk}`` using
   the requested distribution. ``--pin email:persona`` overrides.
3. Seed weekly features + attendance + submissions per student.
4. Optionally train the model + score everyone (``--train --score``).

All seeders are idempotent. Re-runs do not duplicate rows.

Usage::

    cd BE
    python -m ml.data.students.cohort
    python -m ml.data.students.cohort \\
        --distribution thriving=0.25,steady=0.55,at_risk=0.20 \\
        --pin student1@email.com:steady --weeks 16 --train --score
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from collections import Counter
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.core.database import get_supabase  # noqa: E402

from .attendance import seed_attendance_for_student  # noqa: E402
from .features import PERSONAS, seed_student_features  # noqa: E402
from .submissions import seed_submissions_for_student  # noqa: E402


DEFAULT_DISTRIBUTION = {"thriving": 0.20, "steady": 0.60, "at_risk": 0.20}


def _parse_distribution(spec: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise ValueError(f"Invalid distribution token '{token}', expected key=weight")
        key, value = token.split("=", 1)
        key = key.strip().lower()
        if key not in PERSONAS:
            raise ValueError(f"Unknown persona '{key}'. Choose from: {', '.join(PERSONAS)}")
        out[key] = float(value)
    if not out:
        return dict(DEFAULT_DISTRIBUTION)
    total = sum(out.values())
    if total <= 0:
        raise ValueError("Distribution weights must sum to a positive number.")
    return {k: v / total for k, v in out.items()}


def _parse_pins(spec: str | None) -> dict[str, str]:
    if not spec:
        return {}
    out: dict[str, str] = {}
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if ":" not in token:
            raise ValueError(f"Invalid pin '{token}', expected email:persona")
        email, persona = token.split(":", 1)
        email = email.strip().lower()
        persona = persona.strip().lower()
        if persona not in PERSONAS:
            raise ValueError(f"Unknown persona '{persona}' for pin '{email}'.")
        out[email] = persona
    return out


def _persona_for(user_id: str, distribution: dict[str, float]) -> str:
    """Deterministic user_id → persona via a stable hash."""
    digest = hashlib.sha1(f"persona::{user_id}".encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    cumulative = 0.0
    for persona, weight in distribution.items():
        cumulative += weight
        if fraction <= cumulative:
            return persona
    return next(iter(distribution))


def _load_students(sb) -> list[dict[str, Any]]:
    rows = (
        sb.table("users")
        .select("user_id, full_name, email, role_id, is_active")
        .eq("role_id", 3)
        .execute()
        .data
        or []
    )
    return [r for r in rows if r.get("is_active", True) is not False]


def _load_enrollments(sb, user_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """``{user_id: [course_row, ...]}`` joining ``course_enrollments`` → ``courses``."""
    if not user_ids:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for i in range(0, len(user_ids), 200):
        batch = user_ids[i : i + 200]
        rows = (
            sb.table("course_enrollments")
            .select(
                "student_id, courses("
                "id, course_code, title, "
                "course_session, course_session_date, course_session_duration, "
                "course_start_date, course_end_date, course_occurences, lecturer_id"
                ")"
            )
            .in_("student_id", batch)
            .execute()
            .data
            or []
        )
        for r in rows:
            uid = str(r.get("student_id") or "")
            course = r.get("courses")
            if not uid or not course or course.get("id") is None:
                continue
            out.setdefault(uid, []).append(course)
    return out


async def _seed_one_student(
    sb,
    *,
    user: dict[str, Any],
    courses: list[dict[str, Any]],
    persona: str,
    weeks: int,
    ignore_course_window: bool,
    skip_features: bool,
    skip_attendance: bool,
    skip_submissions: bool,
    log_prefix: str,
) -> dict[str, int]:
    user_id = str(user["user_id"])
    counts = {"features": 0, "attendance": 0, "submissions": 0}

    if not skip_features:
        counts["features"] = await seed_student_features(
            user_id=user_id, courses=courses, persona_name=persona,
            weeks=weeks, ignore_course_window=ignore_course_window, log_prefix="",
        )

    if not skip_attendance:
        counts["attendance"] = await asyncio.to_thread(
            seed_attendance_for_student, sb, user_id=user_id, courses=courses,
            persona_name=persona, log_prefix="",
        )

    if not skip_submissions:
        counts["submissions"] = await asyncio.to_thread(
            seed_submissions_for_student, sb, user_id=user_id, courses=courses,
            persona_name=persona, log_prefix="",
        )

    print(
        f"{log_prefix}{user.get('full_name') or user_id} [{persona}] "
        f"courses={len(courses)} "
        f"features={counts['features']} "
        f"attendance={counts['attendance']} "
        f"submissions={counts['submissions']}"
    )
    return counts


def _run_module(module: str, *cli_args: str) -> int:
    """Invoke another module's ``main()`` in-process."""
    import importlib
    import runpy

    saved_argv = sys.argv[:]
    try:
        sys.argv = [module] + list(cli_args)
        try:
            mod = importlib.import_module(module)
        except ImportError:
            runpy.run_module(module, run_name="__main__")
            return 0
        if hasattr(mod, "main"):
            try:
                mod.main()
            except SystemExit as exc:
                code = exc.code or 0
                return int(code) if isinstance(code, int) else 0
            return 0
        runpy.run_module(module, run_name="__main__")
        return 0
    finally:
        sys.argv = saved_argv


async def _run(args: argparse.Namespace) -> int:
    sb = get_supabase(service_role=True)
    if sb is None:
        raise SystemExit("Supabase service-role client not configured (SUPABASE_SERVICE_ROLE_KEY).")

    distribution = _parse_distribution(args.distribution)
    pins = _parse_pins(args.pin)

    students = _load_students(sb)
    if args.limit:
        students = students[: args.limit]
    if not students:
        print("No students with role_id=3 found.")
        return 1

    user_ids = [str(s["user_id"]) for s in students]
    enrollments = _load_enrollments(sb, user_ids)

    persona_counts: Counter[str] = Counter()
    enrolled_only = [s for s in students if enrollments.get(str(s["user_id"]))]
    skipped_no_courses = len(students) - len(enrolled_only)

    print("=" * 72)
    print("Demo cohort seeder")
    print(f"  Students with role=student   : {len(students)}")
    print(f"  Students with at least 1 course: {len(enrolled_only)}")
    print(
        f"  Persona distribution         : "
        + ", ".join(f"{k}={v:.0%}" for k, v in distribution.items())
    )
    if pins:
        print(f"  Persona pins                 : {pins}")
    print(
        f"  Weeks of weekly features     : {args.weeks}"
        + (" (ignoring course window)" if args.ignore_course_window else "")
    )
    print(
        f"  Skips                        : "
        f"features={args.skip_features} "
        f"attendance={args.skip_attendance} "
        f"submissions={args.skip_submissions}"
    )
    print("=" * 72)

    totals = {"features": 0, "attendance": 0, "submissions": 0}
    for idx, student in enumerate(enrolled_only, start=1):
        user_id = str(student["user_id"])
        email = (student.get("email") or "").lower()
        persona = pins.get(email) or _persona_for(user_id, distribution)
        persona_counts[persona] += 1
        prefix = f"[{idx:>3}/{len(enrolled_only)}] "
        try:
            counts = await _seed_one_student(
                sb,
                user=student,
                courses=enrollments[user_id],
                persona=persona,
                weeks=args.weeks,
                ignore_course_window=args.ignore_course_window,
                skip_features=args.skip_features,
                skip_attendance=args.skip_attendance,
                skip_submissions=args.skip_submissions,
                log_prefix=prefix,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"{prefix}{student.get('full_name') or user_id} [{persona}] FAILED: {exc!r}")
            continue
        for key, value in counts.items():
            totals[key] += value

    print("=" * 72)
    print("Seeding totals")
    print(
        f"  Personas assigned : "
        + ", ".join(f"{k}={persona_counts[k]}" for k in sorted(persona_counts))
    )
    print(f"  Skipped (no courses): {skipped_no_courses}")
    print(f"  student_weekly_features upserts: {totals['features']}")
    print(f"  course_attendance inserts      : {totals['attendance']}")
    print(f"  assignment_submissions inserts : {totals['submissions']}")
    print("=" * 72)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Cohort-wide demo seeder: weekly features + attendance + graded "
            "submissions for every active student."
        )
    )
    p.add_argument("--distribution", default="thriving=0.20,steady=0.60,at_risk=0.20",
                   help="Comma-separated 'persona=weight' pairs (weights are normalised).")
    p.add_argument("--pin", default="",
                   help="Comma-separated 'email:persona' overrides.")
    p.add_argument("--weeks", type=int, default=16)
    p.add_argument("--ignore-course-window", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="Process only the first N students (debug).")
    p.add_argument("--skip-features", action="store_true")
    p.add_argument("--skip-attendance", action="store_true")
    p.add_argument("--skip-submissions", action="store_true")
    p.add_argument("--train", action="store_true", help="Run training after seeding.")
    p.add_argument("--score", action="store_true", help="Run scoring after seeding.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    rc = asyncio.run(_run(args))
    # train/score must happen after the asyncio loop closes — those CLIs
    # spin up their own loops.
    if rc == 0 and args.train:
        print("\n>>> Training dropout model (ml.training.train)…")
        _run_module("ml.training.train")
    if rc == 0 and args.score:
        print("\n>>> Scoring all students (ml.training.predict)…")
        _run_module("ml.training.predict")
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
