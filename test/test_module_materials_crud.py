#!/usr/bin/env python3
"""E2E: upload/update/delete module materials on Supabase Storage.

Default run keeps data for inspection and does not perform any cleanup/delete.
Cleanup is separate: --cleanup-only or optional --teardown-after.

Run:
  BE/venv/bin/python BE/test/test_module_materials_crud.py
  BE/venv/bin/python BE/test/test_module_materials_crud.py --cleanup-only
  BE/venv/bin/python BE/test/test_module_materials_crud.py --delete-material-after
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import requests

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")
E2E_COURSE_CODE = "E2E-MAT-SCENARIOS"

ADMIN = {"email": os.environ.get("ADMIN_EMAIL", "learnez@email.com"), "password": os.environ.get("ADMIN_PASSWORD", "123456")}
LECTURER = {
    "email": os.environ.get("LECTURER_EMAIL", "lecturer1@email.com"),
    "password": os.environ.get("LECTURER_PASSWORD", "123456"),
}


def login(payload: dict) -> str:
    r = requests.post(f"{BASE}/api/iam/login", json=payload, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"login failed {payload['email']}: {r.status_code} {r.text}")
    return r.json()["access_token"]


def cleanup_e2e_material_course(admin_token: str) -> None:
    ah = {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}
    listed = requests.get(f"{BASE}/api/courses/", headers={"Authorization": f"Bearer {admin_token}"}, timeout=30)
    if listed.status_code != 200:
        print("cleanup list courses", listed.status_code, listed.text[:200], file=sys.stderr)
        return
    for row in listed.json():
        if row.get("course_code") != E2E_COURSE_CODE:
            continue
        cid = row["id"]
        mods = requests.get(f"{BASE}/api/courses/{cid}/modules", headers={"Authorization": f"Bearer {admin_token}"}, timeout=30)
        if mods.status_code == 200:
            for m in mods.json():
                requests.delete(f"{BASE}/api/courses/{cid}/modules/{m['id']}", headers=ah, timeout=30)
        enr = requests.get(f"{BASE}/api/enrollment/{cid}/students", headers={"Authorization": f"Bearer {admin_token}"}, timeout=30)
        if enr.status_code == 200:
            for e in enr.json():
                sid = e.get("student_id")
                if sid:
                    requests.delete(f"{BASE}/api/enrollment/{cid}/students/{sid}", headers=ah, timeout=30)
        requests.delete(f"{BASE}/api/courses/{cid}", headers=ah, timeout=30)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="E2E for module materials or cleanup only.")
    parser.add_argument("--cleanup-only", action="store_true", help="Only delete E2E materials test data; do not run scenarios.")
    parser.add_argument("--teardown-after", action="store_true", help="Delete course/module after successful scenario run.")
    parser.add_argument(
        "--delete-material-after",
        action="store_true",
        help="Delete uploaded material at end of scenario. Off by default for strict create/cleanup separation.",
    )
    parser.add_argument(
        "--start-cleanup",
        action="store_true",
        help="Before scenarios, remove existing E2E data. Off by default for strict create/cleanup separation.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    admin_t = login(ADMIN)
    ah = {"Authorization": f"Bearer {admin_t}", "Content-Type": "application/json"}
    if args.cleanup_only:
        print("cleanup-only: removing E2E-MAT-SCENARIOS data")
        cleanup_e2e_material_course(admin_t)
        return 0

    if args.start_cleanup:
        print("start cleanup: removing stale E2E-MAT-SCENARIOS data")
        cleanup_e2e_material_course(admin_t)

    acc = requests.get(f"{BASE}/api/iam/accounts/", headers={"Authorization": f"Bearer {admin_t}"}, timeout=30)
    if acc.status_code != 200:
        print("accounts failed", acc.status_code, acc.text)
        return 1
    lecturer = next((u for u in acc.json() if (u.get("email") or "").lower() == LECTURER["email"].lower()), None)

    code = E2E_COURSE_CODE
    body = {
        "title": "Materials E2E",
        "description": "materials CRUD",
        "course_code": code,
        "semester": "1",
        "academic_year": "2025-2026",
        "is_complete": False,
    }
    if lecturer:
        body["lecturer_id"] = lecturer["id"]
    cr = requests.post(f"{BASE}/api/courses/", headers=ah, json=body, timeout=30)
    print("create course", cr.status_code)
    if cr.status_code != 201:
        print(cr.text)
        return 1
    course_id = cr.json()["id"]

    uploader = ah
    if lecturer:
        try:
            lt = login(LECTURER)
            uploader = {"Authorization": f"Bearer {lt}"}
        except RuntimeError:
            pass

    mr = requests.post(
        f"{BASE}/api/courses/{course_id}/modules",
        headers={**uploader, "Content-Type": "application/json"},
        json={"title": "Week 1", "description": "materials"},
        timeout=30,
    )
    print("create module", mr.status_code)
    if mr.status_code != 201:
        print(mr.text)
        return 1
    module_id = mr.json()["id"]

    up = requests.post(
        f"{BASE}/api/content/modules/{module_id}/materials",
        headers=uploader,
        data={"name": "Intro", "description": "Week 1 reading"},
        files={"file": ("intro.txt", b"hello material", "text/plain")},
        timeout=30,
    )
    print("upload", up.status_code, up.text[:250])
    if up.status_code != 201:
        return 1
    mid = up.json()["id"]

    # over-limit check: server default max (10MB) — payload one byte over
    limit_bytes = 10 * 1024 * 1024
    too_big = requests.post(
        f"{BASE}/api/content/modules/{module_id}/materials",
        headers=uploader,
        files={"file": ("big.bin", b"x" * (limit_bytes + 1), "application/octet-stream")},
        timeout=120,
    )
    print("upload too big (expect 413)", too_big.status_code)
    if too_big.status_code != 413:
        return 1

    ls = requests.get(
        f"{BASE}/api/content/modules/{module_id}/materials",
        headers={"Authorization": uploader["Authorization"]},
        timeout=30,
    )
    print("list", ls.status_code)
    if ls.status_code != 200:
        return 1

    upd = requests.put(
        f"{BASE}/api/content/materials/{mid}",
        headers=uploader,
        data={"name": "Intro v2", "description": "Updated"},
        files={"file": ("intro-v2.txt", b"updated material", "text/plain")},
        timeout=30,
    )
    print("update", upd.status_code)
    if upd.status_code != 200:
        print(upd.text)
        return 1

    if args.delete_material_after:
        dele = requests.delete(
            f"{BASE}/api/content/materials/{mid}",
            headers={"Authorization": uploader["Authorization"]},
            timeout=30,
        )
        print("delete material", dele.status_code)
        if dele.status_code != 204:
            return 1
    else:
        print("keep material for inspection", mid)

    if args.teardown_after:
        requests.delete(
            f"{BASE}/api/courses/{course_id}/modules/{module_id}",
            headers=ah,
            timeout=30,
        )
        requests.delete(f"{BASE}/api/courses/{course_id}", headers=ah, timeout=30)
    else:
        print(
            f"Data kept for inspection (course_id={course_id}, module_id={module_id}, material_id={mid}). "
            "Remove with: BE/venv/bin/python BE/test/test_module_materials_crud.py --cleanup-only"
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except requests.RequestException as e:
        print(e, file=sys.stderr)
        sys.exit(1)
