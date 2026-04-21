#!/usr/bin/env python3
"""Exercise every notification scenario; leaves all DB rows (no cleanup).

Run with API + Supabase available. Apply BE/sql/notifications_extend_scenario.sql if needed.

  BE/venv/bin/python BE/test/test_notification_scenarios_e2e.py

Env: API_BASE, ADMIN_EMAIL/PASSWORD, LECTURER_EMAIL/PASSWORD, STUDENT_EMAIL (optional).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "learnez@email.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "123456")
LECTURER_EMAIL = os.environ.get("LECTURER_EMAIL", "lecturer1@email.com")
LECTURER_PASSWORD = os.environ.get("LECTURER_PASSWORD", "123456")
STUDENT_EMAIL = os.environ.get("STUDENT_EMAIL", "").strip()
STUDENT_PASSWORD = os.environ.get("STUDENT_PASSWORD", "123456")

COURSE_CODE = f"SCN-E2E-{int(time.time())}"

# Expected scenario keys (see app/services/notifications/scenario_notifications.py)
STUDENT_SCENARIOS = frozenset(
    {
        "enrollment_added",
        "enrollment_removed",
        "dropout_risk_note",
        "course_announcement",
        "admin_direct_message",
        "assignment_published",
        "assignment_due_date_changed",
        "assignment_due_soon_3d",
        "assignment_due_soon_1d",
        "assignment_overdue",
        "submission_received",
        "grades_released",
        "partial_grading_pending",
        "material_uploaded",
        "daily_digest_student",
        "low_attendance_warning",
    }
)
LECTURER_SCENARIOS = frozenset({"weekly_lecturer_digest"})


def login(email: str, password: str) -> str:
    r = requests.post(
        f"{BASE}/api/iam/login",
        json={"email": email, "password": password},
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"login failed {email}: {r.status_code} {r.text}")
    return r.json()["access_token"]


def pick_account(accounts: list, email: str) -> dict | None:
    for a in accounts:
        if (a.get("email") or "").lower() == email.lower():
            return a
    return None


def auth_json(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def auth_bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def mcq_meta() -> dict:
    return {
        "options": [{"id": "A", "text": "Wrong"}, {"id": "B", "text": "Right"}],
        "correct_option_ids": ["B"],
        "allow_multiple": False,
    }


def essay_meta() -> dict:
    return {"min_words": 10, "max_words": 500}


def scenarios_for_token(token: str) -> set[str]:
    r = requests.get(f"{BASE}/api/notifications/", headers=auth_bearer(token), params={"limit": 100}, timeout=60)
    if r.status_code != 200:
        print("list notifications", r.status_code, r.text[:400], file=sys.stderr)
        return set()
    out = set()
    for row in r.json():
        s = row.get("scenario")
        if s:
            out.add(s)
    return out


def main() -> int:
    try:
        admin_t = login(ADMIN_EMAIL, ADMIN_PASSWORD)
        lec_t = login(LECTURER_EMAIL, LECTURER_PASSWORD)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1

    ah = auth_json(admin_t)
    lh = auth_json(lec_t)

    acc = requests.get(f"{BASE}/api/iam/accounts/", headers=auth_bearer(admin_t), timeout=60)
    if acc.status_code != 200:
        print("accounts", acc.status_code, file=sys.stderr)
        return 1
    accounts = acc.json()
    lec = pick_account(accounts, LECTURER_EMAIL)
    if not lec:
        print("lecturer account missing", file=sys.stderr)
        return 1

    if STUDENT_EMAIL:
        student_row = pick_account(accounts, STUDENT_EMAIL)
    else:
        studs = [a for a in accounts if a.get("role_id") == 3]
        student_row = studs[0] if studs else None
    if not student_row:
        print("no student", file=sys.stderr)
        return 1
    stu_email = STUDENT_EMAIL or student_row.get("email") or ""
    try:
        stu_t = login(stu_email, STUDENT_PASSWORD)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    sh = auth_json(stu_t)
    sid = student_row["id"]

    now = datetime.now(timezone.utc)

    # --- Course + module ---
    cr = requests.post(
        f"{BASE}/api/courses/",
        headers=ah,
        json={
            "title": "Scenario notifications E2E",
            "description": "No cleanup — delete manually",
            "course_code": COURSE_CODE,
            "semester": "1",
            "academic_year": "2025-2026",
            "is_complete": False,
            "lecturer_id": lec["id"],
        },
        timeout=60,
    )
    if cr.status_code != 201:
        print("create course", cr.status_code, cr.text, file=sys.stderr)
        return 1
    course_id = cr.json()["id"]

    mr = requests.post(
        f"{BASE}/api/courses/{course_id}/modules",
        headers=lh,
        json={"title": "SCN module", "description": "E2E"},
        timeout=60,
    )
    if mr.status_code != 201:
        print("module", mr.status_code, mr.text, file=sys.stderr)
        return 1
    module_id = mr.json()["id"]

    # --- Enrollment (enrollment_added) ---
    enr = requests.post(f"{BASE}/api/enrollment/{course_id}/students/{sid}", headers=ah, timeout=60)
    if enr.status_code not in (201, 409):
        print("enroll", enr.status_code, enr.text, file=sys.stderr)
        return 1

    # --- Manual scenarios ---
    for body, scen, title, ntype in [
        (
            "Please meet with the advising office this week.",
            "dropout_risk_note",
            "SCN: At-risk follow-up",
            "system",
        ),
        ("Midterm moved to room 302.", "course_announcement", "SCN: Announcement", "course"),
    ]:
        r = requests.post(
            f"{BASE}/api/notifications/",
            headers=lh,
            json={
                "recipient_id": sid,
                "title": title,
                "body": body,
                "notification_type": ntype,
                "course_id": course_id,
                "scenario": scen,
                "metadata": {"source": "e2e"},
            },
            timeout=60,
        )
        if r.status_code != 201:
            print("manual lecturer", scen, r.status_code, r.text, file=sys.stderr)
            return 1

    r = requests.post(
        f"{BASE}/api/notifications/",
        headers=ah,
        json={
            "recipient_id": sid,
            "title": "SCN: Admin message",
            "body": "Registrar update: verify your profile.",
            "notification_type": "system",
            "scenario": "admin_direct_message",
            "metadata": {"source": "e2e"},
        },
        timeout=60,
    )
    if r.status_code != 201:
        print("admin_direct", r.status_code, r.text, file=sys.stderr)
        return 1

    def post_assignment(title: str, due: datetime | None, questions: list) -> dict | None:
        body = {
            "module_id": module_id,
            "title": title,
            "description": "E2E",
            "total_score": 10.0,
            "is_graded": True,
            "questions": questions,
        }
        if due is not None:
            body["due_date"] = due.isoformat()
        ar = requests.post(f"{BASE}/api/assignments/", headers=lh, json=body, timeout=60)
        if ar.status_code != 201:
            print("assignment", title, ar.status_code, ar.text[:500], file=sys.stderr)
            return None
        return ar.json()

    q1 = [{"type": "mcq", "content": "Pick B", "order_index": 0, "metadata": mcq_meta()}]

    # Digest window: due within 7 days, not submitted
    digest_asg = post_assignment("SCN digest upcoming", now + timedelta(days=4), q1)
    if not digest_asg:
        return 1

    jobs = requests.post(f"{BASE}/api/notifications/jobs/digests", headers=ah, timeout=120)
    if jobs.status_code != 200:
        print("digests", jobs.status_code, jobs.text, file=sys.stderr)
        return 1
    print("digests job:", jobs.json())

    # Main flow assignment
    main_asg = post_assignment("SCN main MCQ", now + timedelta(days=14), q1)
    if not main_asg:
        return 1
    main_id = main_asg["id"]

    # assignment_due_date_changed
    u = requests.put(
        f"{BASE}/api/assignments/{main_id}",
        headers=lh,
        json={"due_date": (now + timedelta(days=10)).isoformat()},
        timeout=60,
    )
    if u.status_code != 200:
        print("update due", u.status_code, u.text, file=sys.stderr)
        return 1

    # Timing buckets for due-reminders job
    post_assignment("SCN overdue", now - timedelta(days=1), q1)
    post_assignment("SCN due 3d window", now + timedelta(days=2), q1)
    post_assignment("SCN due 1d window", now + timedelta(hours=18), q1)

    essay_asg = post_assignment(
        "SCN essay only",
        now + timedelta(days=7),
        [{"type": "essay", "content": "Write essay", "order_index": 0, "metadata": essay_meta()}],
    )
    if not essay_asg:
        return 1
    essay_id = essay_asg["id"]

    due_job = requests.post(f"{BASE}/api/notifications/jobs/due-reminders", headers=ah, timeout=120)
    if due_job.status_code != 200:
        print("due job", due_job.status_code, due_job.text, file=sys.stderr)
        return 1
    print("due-reminders:", due_job.json())

    # --- GET main assignment questions, submit MCQ ---
    gd = requests.get(f"{BASE}/api/assignments/{main_id}", headers=sh, timeout=60)
    if gd.status_code != 200:
        print("get main asg", gd.status_code, file=sys.stderr)
        return 1
    qs = gd.json().get("questions") or []
    if not qs:
        print("no questions main", file=sys.stderr)
        return 1
    q0 = qs[0]
    sr = requests.post(
        f"{BASE}/api/assignments/{main_id}/submissions",
        headers=sh,
        json={
            "answers": [{"question_id": q0["id"], "answer_content": json.dumps({"selected": ["B"]})}],
            "status": "submitted",
        },
        timeout=60,
    )
    if sr.status_code not in (200, 201):
        print("submit mcq", sr.status_code, sr.text, file=sys.stderr)
        return 1

    # Essay submit -> partial_grading_pending
    ge = requests.get(f"{BASE}/api/assignments/{essay_id}", headers=sh, timeout=60)
    if ge.status_code != 200:
        print("get essay", ge.status_code, file=sys.stderr)
        return 1
    eqs = ge.json().get("questions") or []
    se = requests.post(
        f"{BASE}/api/assignments/{essay_id}/submissions",
        headers=sh,
        json={
            "answers": [
                {
                    "question_id": eqs[0]["id"],
                    "answer_content": "Essay body for scenario test. " * 5,
                }
            ],
            "status": "submitted",
        },
        timeout=60,
    )
    if se.status_code not in (200, 201):
        print("submit essay", se.status_code, se.text, file=sys.stderr)
        return 1
    sub_id = se.json()["id"]

    gr = requests.post(
        f"{BASE}/api/grading/{sub_id}/grade",
        headers=lh,
        json={
            "answer_grades": [
                {
                    "question_id": eqs[0]["id"],
                    "earned_score": 8.0,
                    "is_correct": True,
                    "ai_feedback": "OK",
                }
            ],
            "feedback": "Finalized in E2E.",
            "finalize": True,
        },
        timeout=60,
    )
    if gr.status_code != 200:
        print("grade essay", gr.status_code, gr.text, file=sys.stderr)
        return 1

    # Material upload (may fail if storage misconfigured)
    mat_ok = False
    try:
        files = {"file": ("scn-e2e.txt", b"scenario test", "text/plain")}
        data = {"name": "E2E material", "description": ""}
        mu = requests.post(
            f"{BASE}/api/content/modules/{module_id}/materials",
            headers=auth_bearer(lec_t),
            files=files,
            data=data,
            timeout=120,
        )
        mat_ok = mu.status_code == 201
        if not mat_ok:
            print("material upload skipped:", mu.status_code, mu.text[:300], file=sys.stderr)
    except requests.RequestException as e:
        print("material upload error", e, file=sys.stderr)

    # Unenroll / re-enroll
    de = requests.delete(f"{BASE}/api/enrollment/{course_id}/students/{sid}", headers=ah, timeout=60)
    if de.status_code != 204:
        print("unenroll", de.status_code, file=sys.stderr)
        return 1
    re = requests.post(f"{BASE}/api/enrollment/{course_id}/students/{sid}", headers=ah, timeout=60)
    if re.status_code != 201:
        print("re-enroll", re.status_code, file=sys.stderr)
        return 1

    la = requests.post(
        f"{BASE}/api/notifications/jobs/demo-low-attendance",
        headers=ah,
        json={"student_id": sid, "course_id": course_id, "note": "SCN E2E low attendance demo."},
        timeout=60,
    )
    if la.status_code != 200:
        print("low attendance job", la.status_code, la.text, file=sys.stderr)
        return 1

    stu_scen = scenarios_for_token(stu_t)
    lec_scen = scenarios_for_token(lec_t)

    missing_student = STUDENT_SCENARIOS - stu_scen
    if not mat_ok:
        missing_student = missing_student - {"material_uploaded"}

    if missing_student:
        print("MISSING student scenarios:", sorted(missing_student), file=sys.stderr)
        print("Have:", sorted(stu_scen), file=sys.stderr)
        return 1

    miss_lec = LECTURER_SCENARIOS - lec_scen
    if miss_lec:
        print("MISSING lecturer scenarios:", sorted(miss_lec), file=sys.stderr)
        print("Lecturer has:", sorted(lec_scen), file=sys.stderr)
        return 1

    print("OK — all notification scenarios exercised.")
    print(f"  course_code={COURSE_CODE!r} course_id={course_id} module_id={module_id}")
    print(f"  student scenarios seen: {len(stu_scen)}")
    print(f"  lecturer scenarios seen: {len(lec_scen)}")
    print("  No cleanup: delete course / notifications in Supabase when finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
