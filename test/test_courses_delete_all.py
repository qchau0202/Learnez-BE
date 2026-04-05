#!/usr/bin/env python3
"""Delete every course (admin). Requires --yes to avoid accidents.

Run: BE/venv/bin/python BE/test/test_courses_delete_all.py --yes
"""

import argparse
import os
import sys

import requests

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")
LOGIN = {"email": "learnez@email.com", "password": "123456"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete all courses via API")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm deletion of all courses",
    )
    args = parser.parse_args()
    if not args.yes:
        print("Refusing to delete: pass --yes to delete every course.", file=sys.stderr)
        print("Example: BE/venv/bin/python BE/test/test_courses_delete_all.py --yes")
        return 2

    login = requests.post(f"{BASE}/api/iam/login", json=LOGIN, timeout=20)
    if login.status_code != 200:
        print("login", login.status_code, login.text)
        return 1
    token = login.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    listed = requests.get(f"{BASE}/api/courses/", headers=auth, timeout=20)
    if listed.status_code != 200:
        print("list failed", listed.status_code, listed.text)
        return 1
    courses = listed.json()
    if not isinstance(courses, list):
        print("Unexpected list response:", courses)
        return 1
    if not courses:
        print("No courses to delete.")
        return 0

    print(f"Deleting {len(courses)} course(s)...")
    failed = 0
    for row in courses:
        cid = row["id"]
        title = row.get("title", "")
        r = requests.delete(f"{BASE}/api/courses/{cid}", headers=auth, timeout=20)
        ok = r.status_code == 204
        print(f"  id={cid} {title!r} -> {r.status_code}")
        if not ok:
            failed += 1
    print("Done." if failed == 0 else f"Done with {failed} failure(s).")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
