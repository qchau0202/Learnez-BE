#!/usr/bin/env python3
"""End-to-end assignment scenarios: MCQ-only, Essay-only, mixed.

By default the test keeps the E2E course/module in the DB for inspection and
does not perform any cleanup.
Tear down is separate: see --cleanup-only and --teardown-after.

Expects API at API_BASE (default http://127.0.0.1:8000).

Env:
  ADMIN_EMAIL / ADMIN_PASSWORD — default learnlez@email.com / 123456
  LECTURER_EMAIL / LECTURER_PASSWORD — default lecturer1@email.com / 123456
  STUDENT_EMAIL / STUDENT_PASSWORD — optional; if unset, uses first role_id=3 account

Course code E2E-ASGN-SCENARIOS:
  - Default run does not delete anything before scenarios.
  - Use --start-cleanup if you explicitly want pre-run cleanup.

Run scenarios (default): BE/venv/bin/python BE/test/test_assignments_crud.py
Remove E2E data only:  BE/venv/bin/python BE/test/test_assignments_crud.py --cleanup-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import requests

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")

E2E_COURSE_CODE = "E2E-ASGN-SCENARIOS"

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "learnez@email.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "123456")
LECTURER_EMAIL = os.environ.get("LECTURER_EMAIL", "lecturer1@email.com")
LECTURER_PASSWORD = os.environ.get("LECTURER_PASSWORD", "123456")
STUDENT_EMAIL = os.environ.get("STUDENT_EMAIL", "").strip()
STUDENT_PASSWORD = os.environ.get("STUDENT_PASSWORD", "123456")


def login(email: str, password: str) -> str:
    r = requests.post(
        f"{BASE}/api/iam/login",
        json={"email": email, "password": password},
        timeout=30,
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


def mcq_options_metadata() -> dict:
    return {
        "options": [
            {"id": "A", "text": "First option"},
            {"id": "B", "text": "Second option (correct)"},
        ],
        "correct_option_ids": ["B"],
        "allow_multiple": False,
    }


def essay_metadata() -> dict:
    return {"min_words": 50, "max_words": 500}


def cleanup_e2e_course(admin_token: str) -> None:
    """Remove E2E course(s) by course_code (modules, enrollments, course)."""
    ah = auth_json(admin_token)
    listed = requests.get(f"{BASE}/api/courses/", headers=auth_bearer(admin_token), timeout=30)
    if listed.status_code != 200:
        print("cleanup: list courses", listed.status_code, listed.text[:200], file=sys.stderr)
        return
    for c in listed.json():
        if c.get("course_code") != E2E_COURSE_CODE:
            continue
        cid = c["id"]
        mods = requests.get(f"{BASE}/api/courses/{cid}/modules", headers=auth_bearer(admin_token), timeout=30)
        if mods.status_code == 200:
            for m in mods.json():
                mid = m["id"]
                dr = requests.delete(
                    f"{BASE}/api/courses/{cid}/modules/{mid}",
                    headers=ah,
                    timeout=30,
                )
                print(f"cleanup: delete module {mid}", dr.status_code)
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
        cr = requests.delete(f"{BASE}/api/courses/{cid}", headers=ah, timeout=30)
        print(f"cleanup: delete course {cid}", cr.status_code)


def teardown_e2e_course_by_ids(admin_token: str, course_id: int, module_id: int) -> bool:
    """Delete module (and assignments), enrollments, then course. Not part of assertions."""
    ah = auth_json(admin_token)
    dm = requests.delete(
        f"{BASE}/api/courses/{course_id}/modules/{module_id}",
        headers=ah,
        timeout=30,
    )
    print(f"teardown: delete module {module_id}", dm.status_code)
    enr_list = requests.get(
        f"{BASE}/api/enrollment/{course_id}/students",
        headers=auth_bearer(admin_token),
        timeout=30,
    )
    if enr_list.status_code == 200:
        for row in enr_list.json():
            sid = row.get("student_id")
            if sid:
                requests.delete(
                    f"{BASE}/api/enrollment/{course_id}/students/{sid}",
                    headers=ah,
                    timeout=30,
                )
    dc = requests.delete(f"{BASE}/api/courses/{course_id}", headers=ah, timeout=30)
    print(f"teardown: delete course {course_id}", dc.status_code)
    return dc.status_code == 204


def post_assignment(headers: dict, module_id: int, title: str, questions: list) -> dict | None:
    due = datetime(2026, 6, 30, 23, 59, tzinfo=timezone.utc).isoformat()
    body = {
        "module_id": module_id,
        "title": title,
        "description": "E2E scenario",
        "due_date": due,
        "total_score": 10.0,
        "is_graded": True,
        "questions": questions,
    }
    r = requests.post(f"{BASE}/api/assignments/", headers=headers, json=body, timeout=30)
    print(f"  create assignment {title!r}", r.status_code)
    if r.status_code != 201:
        print(r.text[:500], file=sys.stderr)
        return None
    return r.json()


def get_assignment(headers: dict, assignment_id: int) -> dict | None:
    r = requests.get(f"{BASE}/api/assignments/{assignment_id}", headers=headers, timeout=30)
    if r.status_code != 200:
        print("  get assignment", r.status_code, r.text[:300], file=sys.stderr)
        return None
    return r.json()


def delete_assignment(headers: dict, assignment_id: int) -> bool:
    r = requests.delete(f"{BASE}/api/assignments/{assignment_id}", headers=headers, timeout=30)
    print(f"  delete assignment {assignment_id}", r.status_code)
    return r.status_code == 204


def student_submit(
    sh: dict,
    assignment_id: int,
    questions: list,
    answer_for_question,
) -> tuple[int | None, bool]:
    answers = []
    for q in questions:
        answers.append(answer_for_question(q))
    r = requests.post(
        f"{BASE}/api/assignments/{assignment_id}/submissions",
        headers=sh,
        json={"answers": answers, "status": "submitted"},
        timeout=30,
    )
    print(f"  student submit ({len(answers)} answers)", r.status_code)
    if r.status_code not in (200, 201):
        print(r.text[:500], file=sys.stderr)
        return None, False
    data = r.json()
    return data.get("id"), len(data.get("answers") or []) == len(answers)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Assignment E2E scenarios or cleanup.")
    p.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Only delete E2E-ASGN-SCENARIOS data (no scenarios). Not counted as the test.",
    )
    p.add_argument(
        "--start-cleanup",
        action="store_true",
        help="Before scenarios, remove existing E2E data. Off by default for strict create/cleanup separation.",
    )
    p.add_argument(
        "--teardown-after",
        action="store_true",
        help="After successful scenarios, delete module, enrollments, and course (optional wipe).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        admin_t = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1

    if args.cleanup_only:
        print("=== Cleanup only (E2E-ASGN-SCENARIOS) — not running scenarios ===")
        cleanup_e2e_course(admin_t)
        return 0

    ah = auth_json(admin_t)
    if args.start_cleanup:
        print("=== Remove stale E2E course (if any) before scenarios ===")
        cleanup_e2e_course(admin_t)

    acc = requests.get(f"{BASE}/api/iam/accounts/", headers=auth_bearer(admin_t), timeout=30)
    if acc.status_code != 200:
        print("list accounts", acc.status_code, acc.text, file=sys.stderr)
        return 1
    accounts = acc.json()

    lec = pick_account(accounts, LECTURER_EMAIL)
    if not lec:
        print(
            f"No account for LECTURER_EMAIL={LECTURER_EMAIL}; lecturer 403 test skipped.",
            file=sys.stderr,
        )

    student_row: dict | None = None
    if STUDENT_EMAIL:
        student_row = pick_account(accounts, STUDENT_EMAIL)
        if not student_row:
            print(f"STUDENT_EMAIL={STUDENT_EMAIL} not found.", file=sys.stderr)
    else:
        students = [a for a in accounts if a.get("role_id") == 3]
        student_row = students[0] if students else None
        if not student_row:
            print("No student accounts; submission scenarios skipped.", file=sys.stderr)

    module_h = ah
    lec_t: str | None = None
    if lec:
        try:
            lec_t = login(LECTURER_EMAIL, LECTURER_PASSWORD)
            module_h = auth_json(lec_t)
        except RuntimeError as e:
            print(e, file=sys.stderr)
            lec = None
            module_h = ah

    course_body = {
        "title": "Assignment E2E Scenarios",
        "description": "MCQ, Essay, mixed — safe to delete",
        "course_code": E2E_COURSE_CODE,
        "semester": "1",
        "academic_year": "2025-2026",
        "is_complete": False,
    }
    if lec:
        course_body["lecturer_id"] = lec["id"]
    cr = requests.post(f"{BASE}/api/courses/", headers=ah, json=course_body, timeout=30)
    print("create course", cr.status_code, cr.text[:200])
    if cr.status_code != 201:
        return 1
    course_id = cr.json()["id"]

    mr = requests.post(
        f"{BASE}/api/courses/{course_id}/modules",
        headers=module_h,
        json={"title": "E2E module", "description": "Single module for all scenarios"},
        timeout=30,
    )
    print("create module", mr.status_code)
    if mr.status_code != 201:
        print(mr.text, file=sys.stderr)
        return 1
    module_id = mr.json()["id"]

    student_t: str | None = None
    sh: dict | None = None
    student_login_email = STUDENT_EMAIL or (student_row.get("email") if student_row else "")
    if student_row and student_login_email:
        try:
            student_t = login(student_login_email, STUDENT_PASSWORD)
            sh = auth_json(student_t)
        except RuntimeError as e:
            print(e, file=sys.stderr)
            student_row = None

    if student_row and sh:
        enr = requests.post(
            f"{BASE}/api/enrollment/{course_id}/students/{student_row['id']}",
            headers=ah,
            timeout=30,
        )
        print("enroll student", enr.status_code)
        if enr.status_code not in (201, 409):
            return 1

    # --- Scenario 1: MCQ only ---
    print("\n=== Scenario 1: MCQ only ===")
    q_mcq = [
        {"type": "mcq", "content": "Choose the correct letter.", "order_index": 0, "metadata": mcq_options_metadata()},
        {"type": "mcq", "content": "Second MCQ.", "order_index": 1, "metadata": mcq_options_metadata()},
    ]
    a1 = post_assignment(module_h, module_id, "E2E — MCQ only", q_mcq)
    if not a1:
        return 1
    aid1 = a1["id"]
    d1 = get_assignment(module_h, aid1)
    if not d1 or len(d1.get("questions") or []) != 2:
        print("MCQ: expected 2 questions", d1, file=sys.stderr)
        delete_assignment(module_h, aid1)
        return 1
    qs1 = d1["questions"]
    if lec and lec_t:
        sub403 = requests.post(
            f"{BASE}/api/assignments/{aid1}/submissions",
            headers=auth_json(lec_t),
            json={"answers": [], "status": "submitted"},
            timeout=30,
        )
        print("  lecturer submit (expect 403)", sub403.status_code)
        if sub403.status_code != 403:
            print(sub403.text, file=sys.stderr)
            delete_assignment(module_h, aid1)
            return 1

    if sh:

        def ans_mcq(q):
            return {
                "question_id": q["id"],
                "answer_content": json.dumps({"selected": ["B"]}),
            }

        sid, ok_ans = student_submit(sh, aid1, qs1, ans_mcq)
        if sid is None or not ok_ans:
            delete_assignment(module_h, aid1)
            return 1

    if not delete_assignment(module_h, aid1):
        return 1

    # --- Scenario 2: Essay only ---
    print("\n=== Scenario 2: Essay only ===")
    q_essay = [
        {
            "type": "essay",
            "content": "Discuss encapsulation in OOP.",
            "order_index": 0,
            "metadata": essay_metadata(),
        },
        {
            "type": "essay",
            "content": "Explain time complexity of merge sort.",
            "order_index": 1,
            "metadata": essay_metadata(),
        },
    ]
    a2 = post_assignment(module_h, module_id, "E2E — Essay only", q_essay)
    if not a2:
        return 1
    aid2 = a2["id"]
    d2 = get_assignment(module_h, aid2)
    if not d2 or len(d2.get("questions") or []) != 2:
        print("Essay: expected 2 questions", file=sys.stderr)
        delete_assignment(module_h, aid2)
        return 1
    qs2 = d2["questions"]
    for q in qs2:
        if (q.get("type") or "").lower() != "essay":
            print("Essay scenario: question type should be essay", q, file=sys.stderr)
            delete_assignment(module_h, aid2)
            return 1

    if sh:

        def ans_essay(q):
            return {"question_id": q["id"], "answer_content": f"Answer for essay question {q['id']}: detailed paragraph."}

        sid2, ok2 = student_submit(sh, aid2, qs2, ans_essay)
        if sid2 is None or not ok2:
            delete_assignment(module_h, aid2)
            return 1
        pr = requests.put(
            f"{BASE}/api/assignments/{aid2}/submissions/{sid2}",
            headers=sh,
            json={
                "answers": [
                    {"question_id": qs2[0]["id"], "answer_content": "Revised essay paragraph one."},
                    {"question_id": qs2[1]["id"], "answer_content": "Revised essay paragraph two."},
                ]
            },
            timeout=30,
        )
        print("  student update essay submission", pr.status_code)
        if pr.status_code != 200:
            print(pr.text, file=sys.stderr)
            delete_assignment(module_h, aid2)
            return 1

    if not delete_assignment(module_h, aid2):
        return 1

    # --- Scenario 3: MCQ + Essay ---
    print("\n=== Scenario 3: MCQ + Essay (mixed) ===")
    q_mix = [
        {"type": "mcq", "content": "Mixed: pick one.", "order_index": 0, "metadata": mcq_options_metadata()},
        {
            "type": "essay",
            "content": "Mixed: short reflection.",
            "order_index": 1,
            "metadata": essay_metadata(),
        },
    ]
    a3 = post_assignment(module_h, module_id, "E2E — MCQ + Essay", q_mix)
    if not a3:
        return 1
    aid3 = a3["id"]
    d3 = get_assignment(module_h, aid3)
    if not d3 or len(d3.get("questions") or []) != 2:
        print("Mixed: expected 2 questions", file=sys.stderr)
        delete_assignment(module_h, aid3)
        return 1
    qs3 = d3["questions"]
    types = [((q.get("type") or "").lower()) for q in qs3]
    if sorted(types) != ["essay", "mcq"]:
        print("Mixed: types should be mcq and essay", types, file=sys.stderr)
        delete_assignment(module_h, aid3)
        return 1

    if sh:

        def ans_mixed(q):
            qt = (q.get("type") or "").lower()
            if qt == "mcq":
                return {"question_id": q["id"], "answer_content": json.dumps({"selected": ["B"]})}
            return {"question_id": q["id"], "answer_content": "Reflection text for mixed essay part."}

        sid3, ok3 = student_submit(sh, aid3, qs3, ans_mixed)
        if sid3 is None or not ok3:
            delete_assignment(module_h, aid3)
            return 1

    if student_row:
        proxy = requests.post(
            f"{BASE}/api/assignments/{aid3}/submissions",
            headers=ah,
            json={
                "student_id": student_row["id"],
                "answers": [{"question_id": qs3[0]["id"], "answer_content": json.dumps({"selected": ["A"]})}],
                "status": "submitted",
            },
            timeout=30,
        )
        print("  admin proxy resubmit (expect 200/201)", proxy.status_code)
        if proxy.status_code not in (200, 201):
            print(proxy.text, file=sys.stderr)
            delete_assignment(module_h, aid3)
            return 1

    if not delete_assignment(module_h, aid3):
        return 1

    print("\nAll assignment E2E scenarios passed.")
    if args.teardown_after:
        print("=== Optional teardown (--teardown-after) ===")
        if not teardown_e2e_course_by_ids(admin_t, course_id, module_id):
            return 1
    else:
        print(
            f"Data kept for inspection (course_id={course_id}, module_id={module_id}). "
            "Remove with: BE/venv/bin/python BE/test/test_assignments_crud.py --cleanup-only"
        )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except requests.RequestException as e:
        print(e, file=sys.stderr)
        sys.exit(1)
