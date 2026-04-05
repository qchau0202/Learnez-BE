#!/usr/bin/env python3
"""Create → list → get → update (no delete — data stays for inspection).

Run: BE/venv/bin/python BE/test/test_courses_crud.py (from parent of BE/) or venv/bin/python test/test_courses_crud.py (from BE/)
Delete all courses separately: BE/test/test_courses_delete_all.py --yes
"""

import os
import sys

import requests

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")
LOGIN = {"email": "learnez@email.com", "password": "123456"}


def main() -> int:
    login = requests.post(f"{BASE}/api/iam/login", json=LOGIN, timeout=20)
    print("login", login.status_code)
    if login.status_code != 200:
        print(login.text)
        return 1
    t = login.json()["access_token"]
    h = {"Authorization": f"Bearer {t}", "Content-Type": "application/json"}

    payload = {
        "title": "Sample Course API Test",
        "description": "Created from API test",
        "course_code": "CS-API-001",
        "semester": "2026A",
        "is_complete": False,
    }
    created = requests.post(f"{BASE}/api/courses/", headers=h, json=payload, timeout=20)
    print("create", created.status_code, created.text[:500])
    if created.status_code != 201:
        return 1
    cid = created.json()["id"]
    auth = {"Authorization": f"Bearer {t}"}

    listed = requests.get(f"{BASE}/api/courses/", headers=auth, timeout=20)
    print("list", listed.status_code, listed.text[:800])

    get1 = requests.get(f"{BASE}/api/courses/{cid}", headers=auth, timeout=20)
    print("get", get1.status_code, get1.text[:500])

    upd = requests.put(
        f"{BASE}/api/courses/{cid}",
        headers=h,
        json={"description": "Updated description"},
        timeout=20,
    )
    print("update", upd.status_code, upd.text[:500])

    print("\n(No delete — inspect data in DB or UI. To remove all: test_courses_delete_all.py --yes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
