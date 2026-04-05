#!/usr/bin/env python3
"""Seed 5 distinct courses. Run: BE/venv/bin/python BE/test/test_courses_seed.py (cwd = parent of BE/)."""

import os
import sys
from datetime import datetime, timezone

import requests

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")
LOGIN = {"email": "learnez@email.com", "password": "123456"}

# Optional: set env LECTURER_UUID to a lecturer_profiles.user_id, or leave unset
LECTURER_UUID = os.environ.get("LECTURER_UUID") or None

COURSES = [
    {
        "title": "Data Structures and Algorithms",
        "description": "Arrays, trees, graphs, sorting, and complexity analysis.",
        "course_code": "CS-201",
        "semester": "2",
        "is_complete": False,
        "academic_year": "2025-2026",
        "schedule": datetime(2025, 9, 1, 8, 0, tzinfo=timezone.utc).isoformat(),
    },
    {
        "title": "Database Systems",
        "description": "Relational model, SQL, normalization, transactions.",
        "course_code": "CS-301",
        "semester": "1",
        "is_complete": False,
        "academic_year": "2025-2026",
        "schedule": datetime(2026, 1, 15, 10, 30, tzinfo=timezone.utc).isoformat(),
    },
    {
        "title": "Software Engineering",
        "description": "Requirements, design patterns, testing, CI/CD.",
        "course_code": "SE-401",
        "semester": "2",
        "is_complete": True,
        "academic_year": "2025-2026",
        "schedule": None,
    },
    {
        "title": "Computer Networks",
        "description": "TCP/IP, routing, HTTP, and security basics.",
        "course_code": "NET-302",
        "semester": "2",
        "is_complete": False,
        "academic_year": "2025-2026",
        "schedule": datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc).isoformat(),
    },
    {
        "title": "Machine Learning Foundations",
        "description": "Supervised learning, evaluation, neural networks intro.",
        "course_code": "AI-450",
        "semester": "1",
        "academic_year": "2025-2026",
        "is_complete": False,
        "schedule": datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc).isoformat(),
    },
]


def main() -> int:
    r = requests.post(f"{BASE}/api/iam/login", json=LOGIN, timeout=20)
    r.raise_for_status()
    token = r.json()["access_token"]
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    ok = 0
    for i, body in enumerate(COURSES, 1):
        payload = {**body}
        if LECTURER_UUID:
            payload["lecturer_id"] = LECTURER_UUID
        resp = requests.post(f"{BASE}/api/courses/", headers=h, json=payload, timeout=20)
        print(i, resp.status_code, resp.text[:200])
        if resp.status_code == 201:
            ok += 1

    listed = requests.get(
        f"{BASE}/api/courses/",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    print("LIST", listed.status_code, listed.text[:1200])
    return 0 if ok == len(COURSES) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as e:
        print(e, file=sys.stderr)
        sys.exit(1)
