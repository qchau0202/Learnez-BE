"""End-to-end demo seeder: gives every student in Supabase a believable
behavioural fingerprint so lecturer/admin analytics views (especially the
"students at risk" lists and the per-course grade distributions) are
populated without depending on real user activity.

Pipeline
--------

1. **Resolve cohort.** Pull every active ``role=student`` user from
   ``public.users`` and the courses they're enrolled in (one batch query
   per stage to keep round-trips down).
2. **Assign personas.** Each student is hashed deterministically into one
   of ``{thriving, steady, at_risk}`` according to the requested
   distribution. ``--pin email:persona`` overrides the assignment for
   specific demo accounts (e.g. ``--pin student1@email.com:at_risk``).
3. **Seed three layers per student.**
     a. ``learnez_ai.student_weekly_features`` (analytics overview, behaviour,
        ML feature input).
     b. ``public.course_attendance`` (lecturer attendance dashboards).
     c. ``public.assignment_submissions`` + answers (grade distribution,
        lecturer "to grade" / "graded" tabs).
4. **Train + score.** Optionally invoke the existing
   ``ml/training/train_dropout_model.py`` and ``ml/training/sample_dropout_predictions.py``
   so the cached ``risk_scores`` collection refreshes immediately.

All seeders are idempotent — re-running this script does **not** duplicate
attendance or submission rows, and weekly features are upserted by
``(user_id, course_id, week_start)``. ``elearning_raw`` is intentionally
left empty: in demo mode we feed the analytics layer directly and the raw
event database stays reserved for production telemetry.

Usage
-----

    cd BE
    # default distribution: 20% thriving / 60% steady / 20% at_risk
    python -m ml.data.seed_demo_cohort

    # custom distribution + train/score in one go
    python -m ml.data.seed_demo_cohort \\
        --distribution thriving=0.25,steady=0.55,at_risk=0.20 \\
        --pin student1@email.com:steady,student2@email.com:at_risk \\
        --weeks 16 --ignore-course-window --train --score
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
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.database import get_supabase  # noqa: E402

from ml.data.seed_demo_attendance import seed_attendance_for_student  # noqa: E402
from ml.data.seed_demo_student import (  # noqa: E402
    PERSONAS,
    seed_student_features,
)
from ml.data.seed_demo_submissions import seed_submissions_for_student  # noqa: E402


# --------------------------------------------------------------------------- #
# Persona assignment
# --------------------------------------------------------------------------- #

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
    # Normalise so callers can pass either probabilities (0..1) or weights.
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
    """Deterministically map a user_id to a persona using a stable hash so the
    same student keeps the same behavioural profile across re-runs.
    """
    digest = hashlib.sha1(f"persona::{user_id}".encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    cumulative = 0.0
    for persona, weight in distribution.items():
        cumulative += weight
        if fraction <= cumulative:
            return persona
    return next(iter(distribution))  # fallback


# --------------------------------------------------------------------------- #
# Cohort resolution (batched)
# --------------------------------------------------------------------------- #


def _load_students(sb) -> list[dict[str, Any]]:
    """Pull every active learner. Faculty/department live on ``student_profiles``
    but we don't need them here — the seeders work off ``user_id`` and the
    student's enrolled courses (which already carry faculty/department IDs)."""
    rows = (
        sb.table("users")
        .select("user_id, full_name, email, role_id, is_active")
        .eq("role_id", 3)
        .execute()
        .data
        or []
    )
    students = [r for r in rows if r.get("is_active", True) is not False]
    return students


def _load_enrollments(sb, user_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Return ``{user_id: [course_row, ...]}`` for every student in one pass.

    The schema link is ``course_enrollments.student_id`` -> ``users.user_id``;
    we embed the joined ``courses`` row with every column the downstream
    seeders need.
    """
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


# --------------------------------------------------------------------------- #
# Per-student orchestration
# --------------------------------------------------------------------------- #


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
            user_id=user_id,
            courses=courses,
            persona_name=persona,
            weeks=weeks,
            ignore_course_window=ignore_course_window,
            log_prefix="",  # silent inside the per-student loop
        )

    if not skip_attendance:
        counts["attendance"] = await asyncio.to_thread(
            seed_attendance_for_student,
            sb,
            user_id=user_id,
            courses=courses,
            persona_name=persona,
            log_prefix="",
        )

    if not skip_submissions:
        counts["submissions"] = await asyncio.to_thread(
            seed_submissions_for_student,
            sb,
            user_id=user_id,
            courses=courses,
            persona_name=persona,
            log_prefix="",
        )

    print(
        f"{log_prefix}{user.get('full_name') or user_id} [{persona}] "
        f"courses={len(courses)} "
        f"features={counts['features']} "
        f"attendance={counts['attendance']} "
        f"submissions={counts['submissions']}"
    )
    return counts


# --------------------------------------------------------------------------- #
# Optional post-steps: train + score
# --------------------------------------------------------------------------- #


def _run_module(module: str, *cli_args: str) -> int:
    """Invoke a module's ``main()``-style entry point in-process.

    We import the module fresh and call ``sys.argv = [...]; module.main()``
    so we share connections and don't pay the cost of an extra process.
    """
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


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


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
    print(f"Demo cohort seeder")
    print(f"  Students with role=student   : {len(students)}")
    print(f"  Students with at least 1 course: {len(enrolled_only)}")
    print(f"  Persona distribution         : "
          + ", ".join(f"{k}={v:.0%}" for k, v in distribution.items()))
    if pins:
        print(f"  Persona pins                 : {pins}")
    print(f"  Weeks of weekly features     : {args.weeks}"
          + (" (ignoring course window)" if args.ignore_course_window else ""))
    print(f"  Skips                        : "
          f"features={args.skip_features} "
          f"attendance={args.skip_attendance} "
          f"submissions={args.skip_submissions}")
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
    print(f"  Personas assigned : "
          + ", ".join(f"{k}={persona_counts[k]}" for k in sorted(persona_counts)))
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
            "submissions for every active student, with optional model "
            "training and risk scoring."
        )
    )
    p.add_argument(
        "--distribution",
        default="thriving=0.20,steady=0.60,at_risk=0.20",
        help="Comma-separated 'persona=weight' pairs (weights are normalised).",
    )
    p.add_argument(
        "--pin",
        default="",
        help="Comma-separated 'email:persona' overrides (e.g. student1@email.com:at_risk).",
    )
    p.add_argument("--weeks", type=int, default=16, help="Weekly-feature look-back window.")
    p.add_argument(
        "--ignore-course-window",
        action="store_true",
        help="Generate weekly features even before each course's official start_date.",
    )
    p.add_argument("--limit", type=int, default=0, help="Process only the first N students (debug).")
    p.add_argument("--skip-features", action="store_true")
    p.add_argument("--skip-attendance", action="store_true")
    p.add_argument("--skip-submissions", action="store_true")
    p.add_argument("--train", action="store_true", help="Run train_dropout_model after seeding.")
    p.add_argument("--score", action="store_true", help="Run sample_dropout_predictions after seeding.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    rc = asyncio.run(_run(args))
    # Train/score are invoked here, *after* the seeding event loop has fully
    # closed — those scripts use ``asyncio.run`` internally, which would
    # otherwise blow up with "cannot be called from a running event loop".
    if rc == 0 and args.train:
        print("\n>>> Training dropout model (ml.training.train_dropout_model)…")
        _run_module("ml.training.train_dropout_model")
    if rc == 0 and args.score:
        print("\n>>> Scoring all students (ml.training.sample_dropout_predictions)…")
        _run_module("ml.training.sample_dropout_predictions")
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
