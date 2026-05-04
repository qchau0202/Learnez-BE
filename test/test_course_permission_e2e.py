#!/usr/bin/env python3
"""E2E: verify lecturer course-delete permission toggles work and clean up safely.

This scenario snapshots the lecturer's current permission overrides, creates a
mock course, proves delete is forbidden when `course-04` is denied, then proves
delete succeeds once `course-04` is granted. The lecturer's original overrides
are restored in a finally block and the mock course is removed if it still
exists.

Run:
  BE/venv/bin/python BE/test/test_course_permission_e2e.py
  BE/venv/bin/python BE/test/test_course_permission_e2e.py --cleanup-only

Env:
  API_BASE, ADMIN_EMAIL/PASSWORD, LECTURER_EMAIL/PASSWORD
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import requests

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "learnez@email.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "123456")
LECTURER_EMAIL = os.environ.get("LECTURER_EMAIL", "lecturer1@email.com")
LECTURER_PASSWORD = os.environ.get("LECTURER_PASSWORD", "123456")

COURSE_DELETE_PERMISSION_ID = 4
COURSE_CODE_PREFIX = "E2E-COURSE-PERM-"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="E2E for course delete permission management.")
    parser.add_argument("--cleanup-only", action="store_true", help="Only remove stale E2E permission test courses.")
    return parser.parse_args(argv)


def login(email: str, password: str) -> str:
    res = requests.post(
        f"{BASE}/api/iam/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    if res.status_code != 200:
        raise RuntimeError(f"login failed {email}: {res.status_code} {res.text}")
    return res.json()["access_token"]


def auth_bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def auth_json(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def pick_account(accounts: list[dict], email: str) -> dict | None:
    for account in accounts:
        if (account.get("email") or "").lower() == email.lower():
            return account
    return None


def list_accounts(admin_token: str) -> list[dict]:
    res = requests.get(f"{BASE}/api/iam/accounts/", headers=auth_bearer(admin_token), timeout=30)
    if res.status_code != 200:
        raise RuntimeError(f"list accounts failed: {res.status_code} {res.text}")
    return res.json()


def get_permission_overrides(admin_token: str, user_id: str) -> list[dict]:
    res = requests.get(
        f"{BASE}/api/iam/rbac/users/{user_id}/permission-overrides",
        headers=auth_bearer(admin_token),
        timeout=30,
    )
    if res.status_code != 200:
        raise RuntimeError(f"get overrides failed for {user_id}: {res.status_code} {res.text}")
    payload = res.json()
    return payload.get("overrides", [])


def save_permission_overrides_by_email(admin_token: str, email: str, overrides: list[dict]) -> None:
    res = requests.put(
        f"{BASE}/api/iam/rbac/users/permission-overrides/by-email",
        headers=auth_json(admin_token),
        params={"email": email},
        json={"overrides": overrides},
        timeout=30,
    )
    if res.status_code != 200:
        raise RuntimeError(f"save overrides failed for {email}: {res.status_code} {res.text}")


def set_permission_state(overrides: list[dict], permission_id: int, is_allowed: bool) -> list[dict]:
    payload = [
        {"permission_id": row["permission_id"], "is_allowed": bool(row["is_allowed"])}
        for row in overrides
        if row.get("permission_id") != permission_id
    ]
    payload.append({"permission_id": permission_id, "is_allowed": is_allowed})
    payload.sort(key=lambda row: row["permission_id"])
    return payload


def create_course(admin_token: str, lecturer_id: str) -> tuple[int, str]:
    course_code = f"{COURSE_CODE_PREFIX}{time.time_ns()}"
    body = {
        "title": "Course Permission E2E",
        "description": "Temporary mock course for RBAC delete verification",
        "course_code": course_code,
        "semester": "1",
        "academic_year": "2025-2026",
        "is_complete": False,
        "lecturer_id": lecturer_id,
    }
    res = requests.post(
        f"{BASE}/api/courses/",
        headers=auth_json(admin_token),
        json=body,
        timeout=30,
    )
    if res.status_code != 201:
        raise RuntimeError(f"create course failed: {res.status_code} {res.text}")
    created = res.json()
    return created["id"], course_code


def delete_course_as_admin(admin_token: str, course_id: int) -> None:
    res = requests.delete(
        f"{BASE}/api/courses/{course_id}",
        headers=auth_json(admin_token),
        timeout=30,
    )
    if res.status_code not in (204, 404):
        raise RuntimeError(f"cleanup delete failed for course {course_id}: {res.status_code} {res.text}")


def cleanup_stale_courses(admin_token: str) -> None:
    res = requests.get(f"{BASE}/api/courses/", headers=auth_bearer(admin_token), timeout=30)
    if res.status_code != 200:
        raise RuntimeError(f"cleanup list courses failed: {res.status_code} {res.text}")

    for course in res.json():
        code = str(course.get("course_code") or "")
        if not code.startswith(COURSE_CODE_PREFIX):
            continue
        delete_course_as_admin(admin_token, int(course["id"]))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        admin_token = login(ADMIN_EMAIL, ADMIN_PASSWORD)
        lecturer_token = login(LECTURER_EMAIL, LECTURER_PASSWORD)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    accounts = list_accounts(admin_token)
    lecturer = pick_account(accounts, LECTURER_EMAIL)
    if not lecturer:
        print(f"lecturer account not found: {LECTURER_EMAIL}", file=sys.stderr)
        return 1

    original_overrides = get_permission_overrides(admin_token, lecturer["id"])

    if args.cleanup_only:
        cleanup_stale_courses(admin_token)
        print("cleanup-only: removed stale course permission test data")
        return 0

    created_course_id: int | None = None
    created_course_code: str | None = None

    try:
        cleanup_stale_courses(admin_token)
        created_course_id, created_course_code = create_course(admin_token, lecturer["id"])
        print(f"created course {created_course_id} ({created_course_code})")

        deny_payload = set_permission_state(original_overrides, COURSE_DELETE_PERMISSION_ID, False)
        save_permission_overrides_by_email(admin_token, LECTURER_EMAIL, deny_payload)

        forbidden = requests.delete(
            f"{BASE}/api/courses/{created_course_id}",
            headers=auth_json(lecturer_token),
            timeout=30,
        )
        print("lecturer delete without course-04", forbidden.status_code, forbidden.text[:200])
        if forbidden.status_code != 403:
            raise RuntimeError(
                f"expected lecturer delete to be forbidden before grant, got {forbidden.status_code}: {forbidden.text}"
            )

        allow_payload = set_permission_state(original_overrides, COURSE_DELETE_PERMISSION_ID, True)
        save_permission_overrides_by_email(admin_token, LECTURER_EMAIL, allow_payload)

        allowed = requests.delete(
            f"{BASE}/api/courses/{created_course_id}",
            headers=auth_json(lecturer_token),
            timeout=30,
        )
        print("lecturer delete with course-04", allowed.status_code, allowed.text[:200])
        if allowed.status_code != 204:
            raise RuntimeError(
                f"expected lecturer delete to succeed after grant, got {allowed.status_code}: {allowed.text}"
            )

        verify = requests.get(
            f"{BASE}/api/courses/{created_course_id}",
            headers=auth_bearer(admin_token),
            timeout=30,
        )
        print("admin verify deleted course", verify.status_code)
        if verify.status_code != 404:
            raise RuntimeError(f"expected deleted course to be missing, got {verify.status_code}: {verify.text}")

        print("permission e2e passed")
        return 0
    finally:
        try:
            save_permission_overrides_by_email(admin_token, LECTURER_EMAIL, original_overrides)
        except Exception as exc:
            print(f"restore lecturer overrides failed: {exc}", file=sys.stderr)
        if created_course_id is not None:
            try:
                delete_course_as_admin(admin_token, created_course_id)
            except Exception as exc:
                print(f"cleanup course failed: {exc}", file=sys.stderr)
        try:
            cleanup_stale_courses(admin_token)
        except Exception as exc:
            print(f"cleanup stale courses failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())