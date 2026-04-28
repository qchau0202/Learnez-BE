#!/usr/bin/env python3
"""Error case and edge case tests for student file management APIs.

Tests invalid inputs, boundary conditions, and error scenarios.

Run: BE/venv/bin/python BE/test/test_student_files_error_cases.py
"""

from __future__ import annotations

import io
import os
import sys

import requests

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "learnez@email.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "123456")
STUDENT_EMAIL = os.environ.get("STUDENT_EMAIL", "").strip()
STUDENT_PASSWORD = os.environ.get("STUDENT_PASSWORD", "123456")


def login(email: str, password: str) -> tuple[str, int]:
    """Login and return access token (with status for error testing)."""
    r = requests.post(
        f"{BASE}/api/iam/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    if r.status_code == 200:
        return r.json()["access_token"], r.status_code
    return "", r.status_code


def get_or_create_student(admin_token: str) -> tuple[str, str]:
    """Get or create a test student account."""
    h = {"Authorization": f"Bearer {admin_token}"}

    if STUDENT_EMAIL:
        try:
            token, status = login(STUDENT_EMAIL, STUDENT_PASSWORD)
            if status == 200:
                return STUDENT_EMAIL, token
        except Exception:
            pass

    acc = requests.get(f"{BASE}/api/iam/accounts/", headers=h, timeout=30)
    if acc.status_code != 200:
        raise RuntimeError(f"Failed to list accounts: {acc.status_code}")

    students = [u for u in acc.json() if u.get("role") == "Student"]
    if not students:
        raise RuntimeError("No student account found.")

    student = students[0]
    email = student.get("email", "unknown@test.local")
    token, status = login(email, STUDENT_PASSWORD)
    if status == 200:
        return email, token
    raise RuntimeError(f"Failed to login as student {email}")


def main() -> int:
    try:
        admin_token = login(ADMIN_EMAIL, ADMIN_PASSWORD)[0]
        student_email, student_token = get_or_create_student(admin_token)
        h = {"Authorization": f"Bearer {student_token}"}
        hj = {"Authorization": f"Bearer {student_token}", "Content-Type": "application/json"}

        print(f"✓ Logged in as student: {student_email}\n")

        test_results = {"passed": 0, "failed": 0}

        # ====================================================================
        # ERROR TEST 1: Unsupported file type (.exe)
        # ====================================================================
        print("--- ERROR TEST 1: Unsupported file type ---")
        up = requests.post(
            f"{BASE}/api/storage/student-files/files/upload",
            headers={"Authorization": f"Bearer {student_token}"},
            data={"file_title": "Bad File"},
            files={"file": ("malware.exe", b"malicious code", "application/x-msdownload")},
            timeout=30,
        )
        if up.status_code == 400:
            print(f"✓ Correctly rejected .exe file: {up.status_code}")
            test_results["passed"] += 1
        else:
            print(f"✗ Should have rejected .exe file: {up.status_code}")
            test_results["failed"] += 1

        # ====================================================================
        # ERROR TEST 2: File too large (create 6MB file, limit is 5MB)
        # ====================================================================
        print("\n--- ERROR TEST 2: File too large ---")
        large_data = b"x" * (6 * 1024 * 1024)  # 6MB
        up_large = requests.post(
            f"{BASE}/api/storage/student-files/files/upload",
            headers={"Authorization": f"Bearer {student_token}"},
            data={"file_title": "Large File"},
            files={"file": ("large.txt", large_data, "text/plain")},
            timeout=30,
        )
        if up_large.status_code == 413:
            print(f"✓ Correctly rejected oversized file: {up_large.status_code}")
            test_results["passed"] += 1
        else:
            print(f"✗ Should have rejected oversized file: {up_large.status_code}")
            print(f"  Response: {up_large.text[:200]}")
            test_results["failed"] += 1

        # ====================================================================
        # ERROR TEST 3: Get non-existent file
        # ====================================================================
        print("\n--- ERROR TEST 3: Non-existent file ---")
        get_missing = requests.get(
            f"{BASE}/api/storage/student-files/files/99999",
            headers=h,
            timeout=30,
        )
        if get_missing.status_code == 404:
            print(f"✓ Correctly returned 404 for missing file: {get_missing.status_code}")
            test_results["passed"] += 1
        else:
            print(f"✗ Should have returned 404: {get_missing.status_code}")
            test_results["failed"] += 1

        # ====================================================================
        # ERROR TEST 4: Get non-existent folder
        # ====================================================================
        print("\n--- ERROR TEST 4: Non-existent folder ---")
        get_missing_folder = requests.get(
            f"{BASE}/api/storage/student-files/folders/99999",
            headers=h,
            timeout=30,
        )
        if get_missing_folder.status_code == 404:
            print(f"✓ Correctly returned 404 for missing folder: {get_missing_folder.status_code}")
            test_results["passed"] += 1
        else:
            print(f"✗ Should have returned 404: {get_missing_folder.status_code}")
            test_results["failed"] += 1

        # ====================================================================
        # ERROR TEST 5: Create folder with empty name
        # ====================================================================
        print("\n--- ERROR TEST 5: Empty folder name ---")
        empty_folder = requests.post(
            f"{BASE}/api/storage/student-files/folders",
            headers=hj,
            json={"folder_name": "", "description": "Should fail"},
            timeout=30,
        )
        if empty_folder.status_code in (400, 422):  # 422 for Pydantic validation
            print(f"✓ Correctly rejected empty folder name: {empty_folder.status_code}")
            test_results["passed"] += 1
        else:
            print(f"✗ Should have rejected empty name: {empty_folder.status_code}")
            test_results["failed"] += 1

        # ====================================================================
        # ERROR TEST 6: Create duplicate folder name
        # ====================================================================
        print("\n--- ERROR TEST 6: Duplicate folder name ---")
        folder1 = requests.post(
            f"{BASE}/api/storage/student-files/folders",
            headers=hj,
            json={"folder_name": "UniqueFolder", "description": "First"},
            timeout=30,
        )
        if folder1.status_code == 201:
            # Try to create same name again
            folder2 = requests.post(
                f"{BASE}/api/storage/student-files/folders",
                headers=hj,
                json={"folder_name": "UniqueFolder", "description": "Duplicate"},
                timeout=30,
            )
            if folder2.status_code == 400:
                print(f"✓ Correctly rejected duplicate folder name: {folder2.status_code}")
                test_results["passed"] += 1
            else:
                print(f"✗ Should have rejected duplicate: {folder2.status_code}")
                test_results["failed"] += 1
        else:
            print(f"✗ Could not create first folder to test duplicate: {folder1.status_code}")
            test_results["failed"] += 1

        # ====================================================================
        # ERROR TEST 7: Delete non-empty folder without force
        # ====================================================================
        print("\n--- ERROR TEST 7: Delete non-empty folder ---")
        folder_with_file = requests.post(
            f"{BASE}/api/storage/student-files/folders",
            headers=hj,
            json={"folder_name": "FolderWithFile"},
            timeout=30,
        )
        if folder_with_file.status_code == 201:
            folder_id = folder_with_file.json()["id"]

            # Upload file to folder
            up_to_folder = requests.post(
                f"{BASE}/api/storage/student-files/files/upload",
                headers={"Authorization": f"Bearer {student_token}"},
                data={"file_title": "TestFile", "folder_id": folder_id},
                files={"file": ("test.txt", b"content", "text/plain")},
                timeout=30,
            )

            if up_to_folder.status_code == 201:
                # Try to delete folder (should fail - not empty)
                del_empty = requests.delete(
                    f"{BASE}/api/storage/student-files/folders/{folder_id}?force=false",
                    headers=h,
                    timeout=30,
                )
                if del_empty.status_code == 400:
                    print(f"✓ Correctly rejected delete of non-empty folder: {del_empty.status_code}")
                    test_results["passed"] += 1
                else:
                    print(f"✗ Should have rejected delete of non-empty folder: {del_empty.status_code}")
                    test_results["failed"] += 1

        # ====================================================================
        # ERROR TEST 8: Set folder as its own parent (circular reference)
        # ====================================================================
        print("\n--- ERROR TEST 8: Circular folder reference ---")
        folder_self = requests.post(
            f"{BASE}/api/storage/student-files/folders",
            headers=hj,
            json={"folder_name": "SelfRefFolder"},
            timeout=30,
        )
        if folder_self.status_code == 201:
            folder_id = folder_self.json()["id"]

            # Try to set itself as parent
            circular = requests.patch(
                f"{BASE}/api/storage/student-files/folders/{folder_id}",
                headers=hj,
                json={"parent_folder_id": folder_id},
                timeout=30,
            )
            if circular.status_code == 400:
                print(f"✓ Correctly rejected circular reference: {circular.status_code}")
                test_results["passed"] += 1
            else:
                print(f"✗ Should have rejected circular reference: {circular.status_code}")
                test_results["failed"] += 1

        # ====================================================================
        # ERROR TEST 9: Invalid file title (required field)
        # ====================================================================
        print("\n--- ERROR TEST 9: Missing required file title ---")
        no_title = requests.post(
            f"{BASE}/api/storage/student-files/files/upload",
            headers={"Authorization": f"Bearer {student_token}"},
            data={},  # Missing file_title
            files={"file": ("test.txt", b"content", "text/plain")},
            timeout=30,
        )
        if no_title.status_code in (400, 422):
            print(f"✓ Correctly rejected missing file title: {no_title.status_code}")
            test_results["passed"] += 1
        else:
            print(f"✗ Should have rejected missing file title: {no_title.status_code}")
            test_results["failed"] += 1

        # ====================================================================
        # ERROR TEST 10: Delete file twice (idempotent should be safe)
        # ====================================================================
        print("\n--- ERROR TEST 10: Delete file twice ---")
        create_file = requests.post(
            f"{BASE}/api/storage/student-files/files/upload",
            headers={"Authorization": f"Bearer {student_token}"},
            data={"file_title": "FileToDelete"},
            files={"file": ("delete_me.txt", b"content", "text/plain")},
            timeout=30,
        )
        if create_file.status_code == 201:
            file_id = create_file.json()["id"]

            # Delete first time
            del1 = requests.delete(
                f"{BASE}/api/storage/student-files/files/{file_id}",
                headers=h,
                timeout=30,
            )
            if del1.status_code in (200, 204):
                # Delete second time
                del2 = requests.delete(
                    f"{BASE}/api/storage/student-files/files/{file_id}",
                    headers=h,
                    timeout=30,
                )
                if del2.status_code == 404:
                    print(f"✓ Correctly returned 404 for already deleted file: {del2.status_code}")
                    test_results["passed"] += 1
                else:
                    print(f"✗ Should have returned 404 for already deleted: {del2.status_code}")
                    test_results["failed"] += 1

        # ====================================================================
        # ERROR TEST 11: Video/Audio rejection
        # ====================================================================
        print("\n--- ERROR TEST 11: Video/Audio file rejection ---")
        video_attempt = requests.post(
            f"{BASE}/api/storage/student-files/files/upload",
            headers={"Authorization": f"Bearer {student_token}"},
            data={"file_title": "MyVideo"},
            files={"file": ("video.mp4", b"video data", "video/mp4")},
            timeout=30,
        )
        if video_attempt.status_code == 400:
            print(f"✓ Correctly rejected video file: {video_attempt.status_code}")
            test_results["passed"] += 1
        else:
            print(f"✗ Should have rejected video: {video_attempt.status_code}")
            test_results["failed"] += 1

        # ====================================================================
        # SUMMARY
        # ====================================================================
        print("\n" + "=" * 60)
        print(f"TEST SUMMARY: {test_results['passed']} passed, {test_results['failed']} failed")
        print("=" * 60)

        if test_results["failed"] == 0:
            print("✓ All error case tests passed!")
            return 0
        else:
            print(f"✗ {test_results['failed']} test(s) failed")
            return 1

    except Exception as e:
        print(f"\n✗ Fatal error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
