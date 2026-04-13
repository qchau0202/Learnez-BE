#!/usr/bin/env python3
"""E2E notification API: create, list, get, update, recipient patch, bulk ops, RBAC.

Expects API at API_BASE (default http://127.0.0.1:8000) and Supabase table public.notifications.

Course code: E2E-NOTIF — optional module not required.

Run:
  BE/venv/bin/python BE/test/test_notifications_flow.py
  BE/venv/bin/python BE/test/test_notifications_flow.py --start-cleanup
  BE/venv/bin/python BE/test/test_notifications_flow.py --cleanup-only
  BE/venv/bin/python BE/test/test_notifications_flow.py --teardown-after
  BE/venv/bin/python BE/test/test_notifications_flow.py --keep-data
    # leaves notification rows in Supabase (default run deletes them in-scenario)
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

import requests

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")
E2E_COURSE_CODE = "E2E-NOTIF"

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "learnez@email.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "123456")
LECTURER_EMAIL = os.environ.get("LECTURER_EMAIL", "lecturer1@email.com")
LECTURER_PASSWORD = os.environ.get("LECTURER_PASSWORD", "123456")
STUDENT_EMAIL = os.environ.get("STUDENT_EMAIL", "").strip()
STUDENT_PASSWORD = os.environ.get("STUDENT_PASSWORD", "123456")

TITLE_PREFIX = "E2E-NOTIF-"


def login(email: str, password: str) -> str:
    res = requests.post(
        f"{BASE}/api/iam/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    if res.status_code != 200:
        raise RuntimeError(f"login failed {email}: {res.status_code} {res.text}")
    return res.json()["access_token"]


def pick_account(accounts: list, email: str) -> dict | None:
    for a in accounts:
        if (a.get("email") or "").lower() == email.lower():
            return a
    return None


def auth_json(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def auth_bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def cleanup_e2e_course(admin_token: str) -> None:
    ah = auth_json(admin_token)
    listed = requests.get(f"{BASE}/api/courses/", headers=auth_bearer(admin_token), timeout=30)
    if listed.status_code != 200:
        return
    for c in listed.json():
        if c.get("course_code") != E2E_COURSE_CODE:
            continue
        cid = c["id"]
        enr = requests.get(f"{BASE}/api/enrollment/{cid}/students", headers=auth_bearer(admin_token), timeout=30)
        if enr.status_code == 200:
            for row in enr.json():
                sid = row.get("student_id")
                if sid:
                    requests.delete(
                        f"{BASE}/api/enrollment/{cid}/students/{sid}",
                        headers=ah,
                        timeout=30,
                    )
        requests.delete(f"{BASE}/api/courses/{cid}", headers=ah, timeout=30)


def cleanup_notifications_for_recipient(admin_token: str, recipient_id: str) -> None:
    ah = auth_bearer(admin_token)
    r = requests.get(
        f"{BASE}/api/notifications/",
        headers=ah,
        params={"recipient_id": recipient_id, "limit": 100},
        timeout=30,
    )
    if r.status_code != 200:
        print("cleanup notifications list", r.status_code, r.text[:300], file=sys.stderr)
        return
    ids = [n["id"] for n in r.json() if str(n.get("title", "")).startswith(TITLE_PREFIX)]
    if not ids:
        return
    requests.post(
        f"{BASE}/api/notifications/bulk/delete",
        headers=auth_json(admin_token),
        json={"ids": ids},
        timeout=30,
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Notification API E2E or cleanup.")
    p.add_argument("--cleanup-only", action="store_true", help="Remove E2E course and titled notifications.")
    p.add_argument("--start-cleanup", action="store_true", help="Before run: remove stale E2E data.")
    p.add_argument("--teardown-after", action="store_true", help="After success: delete E2E data.")
    p.add_argument(
        "--keep-data",
        action="store_true",
        help="Skip in-test deletes of sample notifications so rows remain in Supabase for inspection.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        admin_t = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1

    ah = auth_json(admin_t)
    acc = requests.get(f"{BASE}/api/iam/accounts/", headers=auth_bearer(admin_t), timeout=30)
    if acc.status_code != 200:
        print("list accounts", acc.status_code, acc.text, file=sys.stderr)
        return 1
    accounts = acc.json()

    if args.cleanup_only:
        print("=== Cleanup only (E2E-NOTIF) ===")
        students = [a for a in accounts if a.get("role_id") == 3]
        for s in students:
            cleanup_notifications_for_recipient(admin_t, s["id"])
        cleanup_e2e_course(admin_t)
        return 0

    if args.start_cleanup:
        for s in [a for a in accounts if a.get("role_id") == 3]:
            cleanup_notifications_for_recipient(admin_t, s["id"])
        cleanup_e2e_course(admin_t)

    lec = pick_account(accounts, LECTURER_EMAIL)
    if not lec:
        print("No lecturer account; abort.", file=sys.stderr)
        return 1
    try:
        lec_t = login(LECTURER_EMAIL, LECTURER_PASSWORD)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    lh = auth_json(lec_t)

    student_row: dict | None = None
    if STUDENT_EMAIL:
        student_row = pick_account(accounts, STUDENT_EMAIL)
    else:
        students = [a for a in accounts if a.get("role_id") == 3]
        student_row = students[0] if students else None
    if not student_row:
        print("No student account.", file=sys.stderr)
        return 1
    student_email = STUDENT_EMAIL or student_row.get("email") or ""
    try:
        stu_t = login(student_email, STUDENT_PASSWORD)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    sh = auth_json(stu_t)
    sid = student_row["id"]

    other_student = next((a for a in accounts if a.get("role_id") == 3 and a["id"] != sid), None)

    course_body = {
        "title": "Notification E2E",
        "description": "Safe to delete",
        "course_code": E2E_COURSE_CODE,
        "semester": "1",
        "academic_year": "2025-2026",
        "is_complete": False,
        "lecturer_id": lec["id"],
    }
    cr = requests.post(f"{BASE}/api/courses/", headers=ah, json=course_body, timeout=30)
    if cr.status_code != 201:
        print("create course", cr.status_code, cr.text, file=sys.stderr)
        return 1
    course_id = cr.json()["id"]

    enr = requests.post(f"{BASE}/api/enrollment/{course_id}/students/{sid}", headers=ah, timeout=30)
    if enr.status_code not in (201, 409):
        print("enroll", enr.status_code, enr.text, file=sys.stderr)
        return 1

    created_ids: list[int] = []

    def post_notif(headers: dict, body: dict) -> tuple[int | None, int]:
        r = requests.post(f"{BASE}/api/notifications/", headers=headers, json=body, timeout=30)
        if r.status_code != 201:
            return None, r.status_code
        j = r.json()
        created_ids.append(j["id"])
        return j["id"], r.status_code

    # Admin → student (system, no course)
    n1_body = {
        "recipient_id": sid,
        "title": f"{TITLE_PREFIX}admin-system",
        "body": "Hello from admin",
        "notification_type": "system",
        "course_id": None,
    }
    n1_id, sc = post_notif(ah, n1_body)
    if n1_id is None:
        print("admin create", sc, file=sys.stderr)
        return 1

    # Student list & get
    lr = requests.get(f"{BASE}/api/notifications/", headers=auth_bearer(stu_t), timeout=30)
    if lr.status_code != 200:
        print("student list", lr.status_code, file=sys.stderr)
        return 1
    if not any(x.get("id") == n1_id for x in lr.json()):
        print("student list missing notification", file=sys.stderr)
        return 1

    g1 = requests.get(f"{BASE}/api/notifications/{n1_id}", headers=auth_bearer(stu_t), timeout=30)
    if g1.status_code != 200:
        print("student get", g1.status_code, file=sys.stderr)
        return 1

    # Student cannot list another user's notifications via recipient_id (Admin-only for others)
    admin_acc = pick_account(accounts, ADMIN_EMAIL)
    if admin_acc:
        bad_q = requests.get(
            f"{BASE}/api/notifications/",
            headers=auth_bearer(stu_t),
            params={"recipient_id": admin_acc["id"]},
            timeout=30,
        )
        if bad_q.status_code != 403:
            print("expected 403 for student filtering by another recipient_id", bad_q.status_code, file=sys.stderr)
            return 1

    # Own recipient_id is allowed (redundant with default scope)
    ok_self = requests.get(
        f"{BASE}/api/notifications/",
        headers=auth_bearer(stu_t),
        params={"recipient_id": sid},
        timeout=30,
    )
    if ok_self.status_code != 200:
        print("expected 200 for student recipient_id=self", ok_self.status_code, file=sys.stderr)
        return 1

    # Recipient patch read
    pr = requests.patch(
        f"{BASE}/api/notifications/{n1_id}/recipient",
        headers=sh,
        json={"is_read": True},
        timeout=30,
    )
    if pr.status_code != 200 or not pr.json().get("is_read"):
        print("patch recipient", pr.status_code, pr.text, file=sys.stderr)
        return 1

    # Lecturer → student (course-linked)
    n2_body = {
        "recipient_id": sid,
        "title": f"{TITLE_PREFIX}lecturer-course",
        "body": "Assignment due soon",
        "notification_type": "course",
        "course_id": course_id,
    }
    n2_id, sc2 = post_notif(lh, n2_body)
    if n2_id is None:
        print("lecturer create", sc2, file=sys.stderr)
        return 1

    # Lecturer list includes course notification for their course
    ll = requests.get(f"{BASE}/api/notifications/", headers=auth_bearer(lec_t), timeout=30)
    if ll.status_code != 200 or not any(x.get("id") == n2_id for x in ll.json()):
        print("lecturer list should include course notification", ll.status_code, file=sys.stderr)
        return 1

    # Lecturer full PUT on course notification
    pu = requests.put(
        f"{BASE}/api/notifications/{n2_id}",
        headers=lh,
        json={"title": f"{TITLE_PREFIX}lecturer-course-updated", "body": "Updated body text here"},
        timeout=30,
    )
    if pu.status_code != 200:
        print("lecturer put", pu.status_code, pu.text, file=sys.stderr)
        return 1

    # Bulk mark unread (student)
    br = requests.post(
        f"{BASE}/api/notifications/bulk/mark-unread",
        headers=sh,
        json={"ids": [n1_id, n2_id]},
        timeout=30,
    )
    if br.status_code != 200 or br.json().get("updated") != 2:
        print("bulk mark-unread (both ids belong to student)", br.status_code, br.text, file=sys.stderr)
        return 1

    # Bulk mark read (student) both own
    br2 = requests.post(
        f"{BASE}/api/notifications/bulk/mark-read",
        headers=sh,
        json={"ids": [n1_id, n2_id]},
        timeout=30,
    )
    if br2.status_code != 200 or br2.json().get("updated") != 2:
        print("bulk mark-read", br2.status_code, br2.text, file=sys.stderr)
        return 1

    if not args.keep_data:
        # Lecturer bulk delete: course-linked id
        bd = requests.post(
            f"{BASE}/api/notifications/bulk/delete",
            headers=lh,
            json={"ids": [n2_id]},
            timeout=30,
        )
        if bd.status_code != 200 or bd.json().get("deleted") != 1:
            print("lecturer bulk delete", bd.status_code, bd.text, file=sys.stderr)
            return 1

        # Student deletes own n1
        d1 = requests.delete(f"{BASE}/api/notifications/{n1_id}", headers=sh, timeout=30)
        if d1.status_code != 204:
            print("student delete", d1.status_code, file=sys.stderr)
            return 1

    # Invalid recipient
    bad_recipient = {
        "recipient_id": str(uuid.uuid4()),
        "title": f"{TITLE_PREFIX}x",
        "body": "x",
        "notification_type": "system",
    }
    brc = requests.post(f"{BASE}/api/notifications/", headers=ah, json=bad_recipient, timeout=30)
    if brc.status_code != 400:
        print("expected 400 unknown recipient", brc.status_code, file=sys.stderr)
        return 1

    # Lecturer cannot notify student not enrolled in their courses (other student)
    if other_student:
        forbidden = {
            "recipient_id": other_student["id"],
            "title": f"{TITLE_PREFIX}forbidden",
            "body": "nope",
            "notification_type": "system",
            "course_id": None,
        }
        fr = requests.post(f"{BASE}/api/notifications/", headers=lh, json=forbidden, timeout=30)
        if fr.status_code != 403:
            print("expected 403 lecturer → non-enrolled student", fr.status_code, fr.text, file=sys.stderr)
            return 1

    if args.teardown_after:
        cleanup_notifications_for_recipient(admin_t, sid)
        if other_student:
            cleanup_notifications_for_recipient(admin_t, other_student["id"])
        cleanup_e2e_course(admin_t)

    print("OK — notification E2E passed")
    if args.keep_data:
        print(
            f"  (--keep-data) Rows may remain: notification ids {n1_id}, {n2_id} "
            f"(titles prefixed {TITLE_PREFIX!r}); course {E2E_COURSE_CODE!r} still exists unless you use --teardown-after.",
        )
    else:
        print("  (default) Sample notifications were deleted in-scenario; use --keep-data to leave rows in Supabase.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
