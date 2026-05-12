"""Provision demo student accounts (auth + users + student_profiles).

Generates ``N`` student accounts mirroring the canonical
``student1@email.com`` profile:

* ``users.email``        → ``student{n}@email.com``
* ``users.full_name``    → ``IT Student {n}``
* ``users.role_id``      → 3 (student)
* ``users.created_by``   → admin UUID
* ``student_profiles``   → student_id ``523k{n:04d}``, faculty 1,
                           department 1, class ``25k50201``, major SE,
                           enrolled_year 2025, GPA 7.0, etc.
* Password ``123456`` (override with ``--password``).

Idempotent: skips any ``student{n}@email.com`` already in ``public.users``
and walks ``n`` upward from ``--start`` until ``--count`` new accounts
exist. Re-runs are no-ops.

Usage::

    cd BE
    python -m ml.data.students.provision                  # next 50 missing
    python -m ml.data.students.provision --count 100 --dry-run
    python -m ml.data.students.provision --count 50 --enroll 0
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.core.database import get_supabase  # noqa: E402


# Canonical profile (matches student1@email.com)
ADMIN_USER_ID = "0ee42aa9-05fc-4d71-a11b-22115bdc5202"
ROLE_STUDENT = 3
FACULTY_ID = 1
DEPARTMENT_ID = 1
MAJOR = "Software Engineering"
STUDENT_CLASS = "25k50201"
ENROLLED_YEAR = 2025
DEFAULT_GPA = 7.0
GENDER = "male"
DATE_OF_BIRTH = "2005-01-01"
PHONE_NUMBER = "0909090909"

EMAIL_PATTERN = re.compile(r"^student(\d+)@email\.com$", re.IGNORECASE)


def _svc():
    sb = get_supabase(service_role=True)
    if sb is None:
        raise SystemExit(
            "Missing Supabase service-role configuration. "
            "Export SUPABASE_SERVICE_ROLE_KEY before running the seeder."
        )
    return sb


def _existing_student_indices(sb) -> set[int]:
    """Return every ``n`` already present matching ``student{n}@email.com``."""
    rows = (
        sb.table("users")
        .select("email")
        .ilike("email", "student%@email.com")
        .execute()
        .data
        or []
    )
    out: set[int] = set()
    for r in rows:
        match = EMAIL_PATTERN.match(str(r.get("email") or "").strip())
        if match:
            out.add(int(match.group(1)))
    return out


def _pick_missing_indices(
    *, existing: set[int], count: int, start: int, ceiling: int,
) -> list[int]:
    out: list[int] = []
    n = max(start, 1)
    while len(out) < count and n <= ceiling:
        if n not in existing:
            out.append(n)
        n += 1
    return out


def _build_user_payload(*, user_id: str, n: int, admin_id: str) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "email": f"student{n}@email.com",
        "full_name": f"IT Student {n}",
        "role_id": ROLE_STUDENT,
        "is_active": True,
        "created_by": admin_id,
    }


def _build_profile_payload(*, user_id: str, n: int) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "student_id": f"523k{n:04d}",
        "gender": GENDER,
        "major": MAJOR,
        "enrolled_year": ENROLLED_YEAR,
        "date_of_birth": DATE_OF_BIRTH,
        "current_gpa": DEFAULT_GPA,
        "cumulative_gpa": DEFAULT_GPA,
        "phone_number": PHONE_NUMBER,
        "faculty_id": FACULTY_ID,
        "department_id": DEPARTMENT_ID,
        "class": STUDENT_CLASS,
    }


def _create_account(sb, *, n: int, password: str, admin_id: str) -> dict[str, Any]:
    """Create the auth user + public.users row + student_profiles row."""
    email = f"student{n}@email.com"
    auth_res = sb.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    )
    if not auth_res.user:
        raise RuntimeError(f"auth.admin.create_user returned no user for {email}")
    user_id = auth_res.user.id

    sb.table("users").insert(_build_user_payload(user_id=user_id, n=n, admin_id=admin_id)).execute()
    sb.table("student_profiles").upsert(
        _build_profile_payload(user_id=user_id, n=n), on_conflict="user_id",
    ).execute()

    return {
        "user_id": user_id, "email": email,
        "full_name": f"IT Student {n}", "student_id": f"523k{n:04d}",
    }


def _existing_course_ids(sb) -> list[int]:
    rows = sb.table("courses").select("id").execute().data or []
    return [int(r["id"]) for r in rows if r.get("id") is not None]


def _enroll_student(sb, *, user_id: str, course_ids: list[int]) -> int:
    if not course_ids:
        return 0
    payload = [{"course_id": cid, "student_id": user_id} for cid in course_ids]
    res = sb.table("course_enrollments").upsert(
        payload, on_conflict="course_id,student_id"
    ).execute()
    return len(res.data or [])


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Seed demo student accounts mirroring student1@email.com.",
    )
    p.add_argument("--count", type=int, default=50, help="How many new students (default 50).")
    p.add_argument("--start", type=int, default=1, help="Lowest ``n`` to scan from.")
    p.add_argument("--ceiling", type=int, default=9999, help="Highest ``n`` to scan to.")
    p.add_argument("--password", type=str, default="123456")
    p.add_argument("--admin-id", type=str, default=ADMIN_USER_ID,
                   help="UID stamped into users.created_by.")
    p.add_argument("--enroll", type=int, default=4,
                   help="How many existing courses to randomly enroll each student into (0 = skip).")
    p.add_argument("--seed", type=int, default=42, help="RNG seed for course shuffling.")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    sb = _svc()
    rng = random.Random(args.seed)

    existing = _existing_student_indices(sb)
    plan = _pick_missing_indices(
        existing=existing, count=args.count, start=args.start, ceiling=args.ceiling
    )

    print("=" * 72)
    print("Demo student provisioner")
    print(f"  Existing student{{n}}@email.com indices : {sorted(existing) or '(none)'}")
    if plan:
        plan_preview = f"{plan[:3]}" + (f" … {plan[-3:]}" if len(plan) > 6 else "")
        print(f"  Plan ({len(plan)} new accounts)             : {plan_preview}")
    else:
        print(f"  Plan ({len(plan)} new accounts)             : (nothing to do)")
    print(f"  Password                                : {args.password}")
    print(f"  Admin (users.created_by)                : {args.admin_id}")
    print(
        "  Enroll                                  : "
        + (f"{args.enroll} courses/student" if args.enroll > 0 else "(skipped)")
    )
    print(f"  Dry-run                                 : {args.dry_run}")
    print("=" * 72)

    if not plan:
        return 0

    course_pool: list[int] = []
    if args.enroll > 0 and not args.dry_run:
        course_pool = _existing_course_ids(sb)
        if not course_pool:
            print(
                "WARNING: 'public.courses' is empty; new students will be created "
                "without enrollments. Run ml.data.curriculum.seed first."
            )

    created: list[dict[str, Any]] = []
    failures: list[tuple[int, str]] = []
    for idx, n in enumerate(plan, start=1):
        prefix = f"[{idx:>3}/{len(plan)}]"
        if args.dry_run:
            print(f"{prefix} dry-run -> would create student{n}@email.com  student_id=523k{n:04d}")
            continue

        try:
            row = _create_account(sb, n=n, password=args.password, admin_id=args.admin_id)
        except Exception as exc:  # noqa: BLE001
            failures.append((n, repr(exc)))
            print(f"{prefix} ERROR student{n}@email.com  {exc!r}")
            continue

        created.append(row)
        msg = (
            f"{prefix} ok    {row['email']}  "
            f"student_id={row['student_id']}  uid={row['user_id']}"
        )

        if args.enroll > 0 and course_pool:
            rng.shuffle(course_pool)
            subset = course_pool[: args.enroll]
            try:
                n_enroll = _enroll_student(sb, user_id=row["user_id"], course_ids=subset)
                msg += f"  enrolled={n_enroll}"
            except Exception as exc:  # noqa: BLE001
                msg += f"  enroll-FAILED={exc!r}"
        print(msg)

    print("=" * 72)
    print(f"Done. created={len(created)} failed={len(failures)} dry_run={args.dry_run}")
    if failures:
        for n, err in failures:
            print(f"  FAIL n={n}: {err}")
    if created:
        print("\nCredentials for the new accounts (all share the password above):")
        for row in created[:5]:
            print(f"  {row['email']}")
        if len(created) > 5:
            print(f"  …and {len(created) - 5} more — login with password {args.password!r}.")
        print(
            "\nNext step: populate analytics for the new cohort by running\n"
            "    python -m ml.data.students.cohort"
        )

    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
