#!/usr/bin/env python3
"""E2E grading flow: MCQ auto-grade, mixed partial auto-grade, manual essay grading.

Default run keeps data for inspection (no teardown).
Cleanup is separate: --cleanup-only, optional --start-cleanup before run, --teardown-after.

Expects API at API_BASE (default http://127.0.0.1:8000).

Course code: E2E-GRADING-FLOW

Run:
  BE/venv/bin/python BE/test/test_grading_flow.py
  BE/venv/bin/python BE/test/test_grading_flow.py --start-cleanup   # fresh run if duplicate code exists
  BE/venv/bin/python BE/test/test_grading_flow.py --cleanup-only
  BE/venv/bin/python BE/test/test_grading_flow.py --teardown-after
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import requests

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "learnez@email.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "123456")
LECTURER_EMAIL = os.environ.get("LECTURER_EMAIL", "lecturer1@email.com")
LECTURER_PASSWORD = os.environ.get("LECTURER_PASSWORD", "123456")
STUDENT_EMAIL = os.environ.get("STUDENT_EMAIL", "").strip()
STUDENT_PASSWORD = os.environ.get("STUDENT_PASSWORD", "123456")

COURSE_CODE = "E2E-GRADING-FLOW"


def login(email: str, password: str) -> str:
    res = requests.post(
        f"{BASE}/api/iam/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    if res.status_code != 200:
        raise RuntimeError(f"login failed {email}: {res.status_code} {res.text}")
    return res.json()["access_token"]


def auth_json(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def auth_bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def pick_account(accounts: list[dict], email: str) -> dict | None:
    for account in accounts:
        if (account.get("email") or "").lower() == email.lower():
            return account
    return None


def mcq_metadata() -> dict:
    return {
        "options": [
            {"id": "A", "text": "Wrong"},
            {"id": "B", "text": "Right"},
        ],
        "correct_option_ids": ["B"],
        "allow_multiple": False,
    }


def essay_metadata() -> dict:
    return {"min_words": 20, "max_words": 300}


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def list_accounts(admin_token: str) -> list[dict]:
    res = requests.get(f"{BASE}/api/iam/accounts/", headers=auth_bearer(admin_token), timeout=30)
    if res.status_code != 200:
        raise RuntimeError(f"list accounts failed: {res.status_code} {res.text}")
    return res.json()


def cleanup_course(admin_token: str) -> None:
    headers = auth_json(admin_token)
    listed = requests.get(f"{BASE}/api/courses/", headers=auth_bearer(admin_token), timeout=30)
    if listed.status_code != 200:
        return
    for course in listed.json():
        if course.get("course_code") != COURSE_CODE:
            continue
        cid = course["id"]
        mods = requests.get(f"{BASE}/api/courses/{cid}/modules", headers=auth_bearer(admin_token), timeout=30)
        if mods.status_code == 200:
            for module in mods.json():
                requests.delete(f"{BASE}/api/courses/{cid}/modules/{module['id']}", headers=headers, timeout=30)
        enrollments = requests.get(
            f"{BASE}/api/enrollment/{cid}/students",
            headers=auth_bearer(admin_token),
            timeout=30,
        )
        if enrollments.status_code == 200:
            for row in enrollments.json():
                sid = row.get("student_id")
                if sid:
                    requests.delete(
                        f"{BASE}/api/enrollment/{cid}/students/{sid}",
                        headers=headers,
                        timeout=30,
                    )
        requests.delete(f"{BASE}/api/courses/{cid}", headers=headers, timeout=30)


def create_assignment(headers: dict, module_id: int, title: str, questions: list[dict], total_score: float = 10.0) -> dict:
    due = datetime(2026, 6, 30, 23, 59, tzinfo=timezone.utc).isoformat()
    res = requests.post(
        f"{BASE}/api/assignments/",
        headers=headers,
        json={
            "module_id": module_id,
            "title": title,
            "description": "grading flow",
            "due_date": due,
            "total_score": total_score,
            "is_graded": True,
            "questions": questions,
        },
        timeout=30,
    )
    if res.status_code != 201:
        raise RuntimeError(f"create assignment failed: {res.status_code} {res.text}")
    return res.json()


def get_assignment(headers: dict, assignment_id: int) -> dict:
    res = requests.get(f"{BASE}/api/assignments/{assignment_id}", headers=headers, timeout=30)
    if res.status_code != 200:
        raise RuntimeError(f"get assignment failed: {res.status_code} {res.text}")
    return res.json()


def get_submission(headers: dict, assignment_id: int, submission_id: int) -> dict:
    res = requests.get(
        f"{BASE}/api/assignments/{assignment_id}/submissions/{submission_id}",
        headers=headers,
        timeout=30,
    )
    if res.status_code != 200:
        raise RuntimeError(f"get submission failed: {res.status_code} {res.text}")
    return res.json()


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Grading flow E2E or cleanup only.")
    p.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Only delete E2E-GRADING-FLOW data (no scenarios).",
    )
    p.add_argument(
        "--start-cleanup",
        action="store_true",
        help="Before scenarios, remove existing E2E course (use if course_code already exists).",
    )
    p.add_argument(
        "--teardown-after",
        action="store_true",
        help="After success, delete module, enrollments, and course.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        admin_token = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    except Exception as exc:
        return fail(str(exc))

    if args.cleanup_only:
        print("=== Cleanup only (E2E-GRADING-FLOW) — not running scenarios ===")
        cleanup_course(admin_token)
        return 0

    if args.start_cleanup:
        print("=== Pre-run cleanup (E2E-GRADING-FLOW) ===")
        cleanup_course(admin_token)

    try:
        accounts = list_accounts(admin_token)
    except Exception as exc:
        return fail(str(exc))

    lecturer = pick_account(accounts, LECTURER_EMAIL)
    if not lecturer:
        return fail(f"Lecturer account not found: {LECTURER_EMAIL}")

    if STUDENT_EMAIL:
        student = pick_account(accounts, STUDENT_EMAIL)
    else:
        students = [row for row in accounts if row.get("role_id") == 3]
        student = students[0] if students else None
    if not student:
        return fail("Student account not found")

    try:
        lecturer_token = login(LECTURER_EMAIL, LECTURER_PASSWORD)
        student_token = login(student["email"], STUDENT_PASSWORD)
    except Exception as exc:
        return fail(str(exc))

    admin_h = auth_json(admin_token)
    lecturer_h = auth_json(lecturer_token)
    student_h = auth_json(student_token)

    course_res = requests.post(
        f"{BASE}/api/courses/",
        headers=admin_h,
        json={
            "title": "Grading Flow",
            "description": "E2E grading flow",
            "course_code": COURSE_CODE,
            "semester": "1",
            "academic_year": "2025-2026",
            "lecturer_id": lecturer["id"],
            "is_complete": False,
        },
        timeout=30,
    )
    if course_res.status_code != 201:
        return fail(
            f"create course failed: {course_res.status_code} {course_res.text}\n"
            "Hint: pass --start-cleanup if E2E-GRADING-FLOW already exists."
        )
    course_id = course_res.json()["id"]

    module_res = requests.post(
        f"{BASE}/api/courses/{course_id}/modules",
        headers=lecturer_h,
        json={"title": "Module", "description": "Grading module"},
        timeout=30,
    )
    if module_res.status_code != 201:
        return fail(f"create module failed: {module_res.status_code} {module_res.text}")
    module_id = module_res.json()["id"]

    enroll_res = requests.post(
        f"{BASE}/api/enrollment/{course_id}/students/{student['id']}",
        headers=admin_h,
        timeout=30,
    )
    if enroll_res.status_code not in (201, 409):
        return fail(f"enroll failed: {enroll_res.status_code} {enroll_res.text}")

    mcq_assignment = create_assignment(
        lecturer_h,
        module_id,
        "MCQ Auto Grade",
        [{"type": "mcq", "content": "Choose B", "order_index": 0, "metadata": mcq_metadata()}],
    )
    mcq_detail = get_assignment(lecturer_h, mcq_assignment["id"])
    mcq_question = mcq_detail["questions"][0]
    mcq_submit = requests.post(
        f"{BASE}/api/assignments/{mcq_assignment['id']}/submissions",
        headers=student_h,
        json={
            "answers": [{"question_id": mcq_question["id"], "answer_content": json.dumps({"selected": ["B"]})}],
            "status": "submitted",
        },
        timeout=30,
    )
    if mcq_submit.status_code not in (200, 201):
        return fail(f"submit mcq failed: {mcq_submit.status_code} {mcq_submit.text}")
    mcq_submission = mcq_submit.json()
    if mcq_submission.get("is_corrected") is not True:
        return fail(f"mcq should auto-finish grading: {mcq_submission}")
    if float(mcq_submission.get("final_score") or 0.0) <= 0:
        return fail(f"mcq final_score should be > 0: {mcq_submission}")
    answer_row = (mcq_submission.get("answers") or [{}])[0]
    if answer_row.get("is_correct") is not True:
        return fail(f"mcq answer should be correct: {answer_row}")

    mixed_assignment = create_assignment(
        lecturer_h,
        module_id,
        "Mixed Manual Grade",
        [
            {"type": "mcq", "content": "Choose B", "order_index": 0, "metadata": mcq_metadata()},
            {"type": "essay", "content": "Explain abstraction", "order_index": 1, "metadata": essay_metadata()},
        ],
    )
    mixed_detail = get_assignment(lecturer_h, mixed_assignment["id"])
    questions = mixed_detail["questions"]
    question_by_type = {q["type"].lower(): q for q in questions}
    mixed_submit = requests.post(
        f"{BASE}/api/assignments/{mixed_assignment['id']}/submissions",
        headers=student_h,
        json={
            "answers": [
                {"question_id": question_by_type["mcq"]["id"], "answer_content": json.dumps({"selected": ["B"]})},
                {"question_id": question_by_type["essay"]["id"], "answer_content": "Abstraction hides implementation details."},
            ],
            "status": "submitted",
        },
        timeout=30,
    )
    if mixed_submit.status_code not in (200, 201):
        return fail(f"submit mixed failed: {mixed_submit.status_code} {mixed_submit.text}")
    mixed_submission = mixed_submit.json()
    if mixed_submission.get("is_corrected") is not False:
        return fail(f"mixed submission should wait for manual grading: {mixed_submission}")
    if float(mixed_submission.get("final_score") or 0.0) <= 0:
        return fail(f"mixed submission should retain mcq score before manual grading: {mixed_submission}")

    feedback_res = requests.post(
        f"{BASE}/api/grading/{mixed_submission['id']}/feedback",
        headers=lecturer_h,
        json={"feedback": "Essay content reviewed soon."},
        timeout=30,
    )
    if feedback_res.status_code != 200:
        return fail(f"feedback failed: {feedback_res.status_code} {feedback_res.text}")

    grade_res = requests.post(
        f"{BASE}/api/grading/{mixed_submission['id']}/grade",
        headers=lecturer_h,
        json={
            "answer_grades": [
                {
                    "question_id": question_by_type["essay"]["id"],
                    "earned_score": 5.0,
                    "ai_feedback": "Good explanation with enough detail.",
                }
            ],
            "feedback": "MCQ auto-graded, essay manually graded.",
            "finalize": True,
        },
        timeout=30,
    )
    if grade_res.status_code != 200:
        return fail(f"manual grade failed: {grade_res.status_code} {grade_res.text}")
    graded = grade_res.json()
    if graded.get("is_corrected") is not True:
        return fail(f"manual grade should finalize submission: {graded}")
    if (graded.get("feedback") or "") != "MCQ auto-graded, essay manually graded.":
        return fail(f"submission feedback not saved: {graded}")
    if float(graded.get("final_score") or 0.0) <= 5.0:
        return fail(f"final score should include mcq + essay points: {graded}")

    refreshed = get_submission(lecturer_h, mixed_assignment["id"], mixed_submission["id"])
    essay_answers = [
        row for row in (refreshed.get("answers") or []) if row.get("question_id") == question_by_type["essay"]["id"]
    ]
    if not essay_answers:
        return fail(f"essay answer missing after grading: {refreshed}")
    essay_answer = essay_answers[0]
    if float(essay_answer.get("earned_score") or 0.0) != 5.0:
        return fail(f"essay score mismatch: {essay_answer}")

    print("Grading flow passed.")
    if args.teardown_after:
        print("=== Teardown (--teardown-after) ===")
        cleanup_course(admin_token)
    else:
        print(
            f"Data kept for inspection (course_id={course_id}, module_id={module_id}). "
            "Remove with: BE/venv/bin/python BE/test/test_grading_flow.py --cleanup-only"
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except requests.RequestException as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
