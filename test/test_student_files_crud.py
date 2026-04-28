#!/usr/bin/env python3
"""E2E: Student file management (folders & files) CRUD scenarios.

By default the test keeps the E2E data in the DB for inspection and
does not perform any cleanup.
Tear down is separate: see --cleanup-only and --teardown-after.

Expects API at API_BASE (default http://127.0.0.1:8000).

Env:
  STUDENT_EMAIL / STUDENT_PASSWORD — optional; if unset, uses first role_id=3 account
  ADMIN_EMAIL / ADMIN_PASSWORD — default learnez@email.com / 123456

Run scenarios (default): BE/venv/bin/python BE/test/test_student_files_crud.py
Remove E2E data only:  BE/venv/bin/python BE/test/test_student_files_crud.py --cleanup-only
Run + cleanup:         BE/venv/bin/python BE/test/test_student_files_crud.py --teardown-after
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

import requests

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "learnez@email.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "123456")
STUDENT_EMAIL = os.environ.get("STUDENT_EMAIL", "").strip()
STUDENT_PASSWORD = os.environ.get("STUDENT_PASSWORD", "123456")


def login(email: str, password: str) -> str:
    """Login and return access token."""
    r = requests.post(
        f"{BASE}/api/iam/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"login failed {email}: {r.status_code} {r.text}")
    return r.json()["access_token"]


def get_or_create_student(admin_token: str) -> tuple[str, str]:
    """Get or create a test student account."""
    h = {"Authorization": f"Bearer {admin_token}"}

    # Try to get existing student or use provided STUDENT_EMAIL
    if STUDENT_EMAIL:
        try:
            token = login(STUDENT_EMAIL, STUDENT_PASSWORD)
            return STUDENT_EMAIL, token
        except Exception:
            pass

    # List accounts and find first student
    acc = requests.get(f"{BASE}/api/iam/accounts/", headers=h, timeout=30)
    if acc.status_code != 200:
        raise RuntimeError(f"Failed to list accounts: {acc.status_code}")

    students = [u for u in acc.json() if u.get("role") == "Student"]
    if not students:
        raise RuntimeError("No student account found. Set STUDENT_EMAIL and STUDENT_PASSWORD env vars.")

    student = students[0]
    email = student.get("email", "unknown@test.local")
    try:
        token = login(email, STUDENT_PASSWORD)
        return email, token
    except Exception as e:
        raise RuntimeError(f"Failed to login as student {email}: {e}")


def cleanup_student_files(student_token: str) -> None:
    """Soft-delete all student files and folders for cleanup."""
    h = {"Authorization": f"Bearer {student_token}"}

    # Delete all files
    files_resp = requests.get(f"{BASE}/api/storage/student-files/files?limit=500", headers=h, timeout=30)
    if files_resp.status_code == 200:
        for file_data in files_resp.json().get("items", []):
            fid = file_data.get("id")
            if fid:
                requests.delete(f"{BASE}/api/storage/student-files/files/{fid}", headers=h, timeout=30)
                print(f"  deleted file {fid}")

    # Delete all folders
    folders_resp = requests.get(f"{BASE}/api/storage/student-files/folders", headers=h, timeout=30)
    if folders_resp.status_code == 200:
        for folder_data in folders_resp.json().get("items", []):
            fid = folder_data.get("id")
            if fid:
                requests.delete(f"{BASE}/api/storage/student-files/folders/{fid}", headers=h, timeout=30)
                print(f"  deleted folder {fid}")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="E2E for student file management.")
    parser.add_argument("--cleanup-only", action="store_true", help="Only delete student files/folders; do not run scenarios.")
    parser.add_argument("--teardown-after", action="store_true", help="Delete files/folders after successful scenario run.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        admin_token = login(ADMIN_EMAIL, ADMIN_PASSWORD)
        student_email, student_token = get_or_create_student(admin_token)
        h = {"Authorization": f"Bearer {student_token}"}
        hj = {"Authorization": f"Bearer {student_token}", "Content-Type": "application/json"}

        print(f"✓ Logged in as student: {student_email}")

        if args.cleanup_only:
            print("cleanup-only: removing E2E student files data")
            cleanup_student_files(student_token)
            return 0

        # ====================================================================
        # SCENARIO 1: Create Folders with Hierarchy
        # ====================================================================
        print("\n--- SCENARIO 1: Create Folders ---")

        # Create Main Folder
        folder1_resp = requests.post(
            f"{BASE}/api/storage/student-files/folders",
            headers=hj,
            json={"folder_name": "Assignments", "description": "My assignments"},
            timeout=30,
        )
        print(f"create folder 'Assignments': {folder1_resp.status_code}")
        if folder1_resp.status_code != 201:
            print(f"  error: {folder1_resp.text}")
            return 1
        folder1 = folder1_resp.json()
        folder1_id = folder1["id"]
        print(f"  id={folder1_id}, name={folder1['folder_name']}")

        # Create Second Main Folder
        folder2_resp = requests.post(
            f"{BASE}/api/storage/student-files/folders",
            headers=hj,
            json={"folder_name": "Projects", "description": "My projects"},
            timeout=30,
        )
        print(f"create folder 'Projects': {folder2_resp.status_code}")
        if folder2_resp.status_code != 201:
            print(f"  error: {folder2_resp.text}")
            return 1
        folder2 = folder2_resp.json()
        folder2_id = folder2["id"]

        # Create Nested Folder (under Assignments)
        folder3_resp = requests.post(
            f"{BASE}/api/storage/student-files/folders",
            headers=hj,
            json={
                "folder_name": "Week 1",
                "description": "Week 1 assignments",
                "parent_folder_id": folder1_id,
            },
            timeout=30,
        )
        print(f"create nested folder 'Week 1' (under Assignments): {folder3_resp.status_code}")
        if folder3_resp.status_code != 201:
            print(f"  error: {folder3_resp.text}")
            return 1
        folder3 = folder3_resp.json()
        folder3_id = folder3["id"]

        # ====================================================================
        # SCENARIO 2: List Folders
        # ====================================================================
        print("\n--- SCENARIO 2: List Folders ---")

        # List main folders (no parent)
        list_resp = requests.get(
            f"{BASE}/api/storage/student-files/folders",
            headers=h,
            timeout=30,
        )
        print(f"list folders: {list_resp.status_code}")
        if list_resp.status_code != 200:
            print(f"  error: {list_resp.text}")
            return 1
        main_folders = list_resp.json()
        print(f"  found {main_folders['total']} main folders")
        for f in main_folders.get("items", []):
            print(f"    - {f['folder_name']} (id={f['id']})")

        # List sub-folders
        sub_resp = requests.get(
            f"{BASE}/api/storage/student-files/folders?parent_folder_id={folder1_id}",
            headers=h,
            timeout=30,
        )
        print(f"list sub-folders of Assignments: {sub_resp.status_code}")
        if sub_resp.status_code == 200:
            subfolders = sub_resp.json()
            print(f"  found {subfolders['total']} sub-folders")

        # ====================================================================
        # SCENARIO 3: Get Folder Details
        # ====================================================================
        print("\n--- SCENARIO 3: Get Folder Details ---")

        detail_resp = requests.get(
            f"{BASE}/api/storage/student-files/folders/{folder1_id}",
            headers=h,
            timeout=30,
        )
        print(f"get folder {folder1_id}: {detail_resp.status_code}")
        if detail_resp.status_code != 200:
            print(f"  error: {detail_resp.text}")
            return 1
        folder_detail = detail_resp.json()
        print(f"  name={folder_detail['folder_name']}, file_count={folder_detail.get('file_count', 0)}")

        # ====================================================================
        # SCENARIO 4: Upload Files (Multiple)
        # ====================================================================
        print("\n--- SCENARIO 4: Upload Files ---")

        # File 1: Upload to folder1 (Assignments)
        file1_content = b"This is my assignment 1 submission for Week 1"
        up1 = requests.post(
            f"{BASE}/api/storage/student-files/files/upload",
            headers={"Authorization": f"Bearer {student_token}"},
            data={
                "file_title": "Assignment 1",
                "description": "My first assignment",
                "folder_id": folder1_id,
            },
            files={"file": ("assignment1.txt", file1_content, "text/plain")},
            timeout=30,
        )
        print(f"upload file1 to folder {folder1_id}: {up1.status_code}")
        if up1.status_code != 201:
            print(f"  error: {up1.text}")
            return 1
        file1 = up1.json()
        file1_id = file1["id"]
        print(f"  id={file1_id}, title={file1['file_title']}, size={file1.get('size_bytes')} bytes")

        # File 2: Upload to nested folder (Week 1)
        file2_content = b"This is my solution for problem 1\nThis is my solution for problem 2"
        up2 = requests.post(
            f"{BASE}/api/storage/student-files/files/upload",
            headers={"Authorization": f"Bearer {student_token}"},
            data={
                "file_title": "Week 1 Solutions",
                "description": "Solutions for all problems",
                "folder_id": folder3_id,
            },
            files={"file": ("week1_solutions.txt", file2_content, "text/plain")},
            timeout=30,
        )
        print(f"upload file2 to nested folder {folder3_id}: {up2.status_code}")
        if up2.status_code != 201:
            print(f"  error: {up2.text}")
            return 1
        file2 = up2.json()
        file2_id = file2["id"]

        # File 3: Upload to Main folder (no parent)
        file3_content = b"README: How to use my files..."
        up3 = requests.post(
            f"{BASE}/api/storage/student-files/files/upload",
            headers={"Authorization": f"Bearer {student_token}"},
            data={
                "file_title": "README",
                "description": "General README",
            },
            files={"file": ("README.txt", file3_content, "text/plain")},
            timeout=30,
        )
        print(f"upload file3 to Main folder: {up3.status_code}")
        if up3.status_code != 201:
            print(f"  error: {up3.text}")
            return 1
        file3 = up3.json()
        file3_id = file3["id"]

        # ====================================================================
        # SCENARIO 5: List Files (with pagination)
        # ====================================================================
        print("\n--- SCENARIO 5: List Files ---")

        # List all files
        files_all = requests.get(
            f"{BASE}/api/storage/student-files/files?limit=50",
            headers=h,
            timeout=30,
        )
        print(f"list all files: {files_all.status_code}")
        if files_all.status_code != 200:
            print(f"  error: {files_all.text}")
            return 1
        all_files = files_all.json()
        print(f"  total={all_files['total']}, storage_used_mb={all_files.get('storage_used_mb', 0)}")
        for f in all_files.get("items", []):
            print(f"    - {f['file_title']} ({f.get('size_bytes', 0)} bytes)")

        # List files in specific folder
        files_in_folder = requests.get(
            f"{BASE}/api/storage/student-files/files?folder_id={folder1_id}",
            headers=h,
            timeout=30,
        )
        print(f"list files in Assignments folder: {files_in_folder.status_code}")
        if files_in_folder.status_code == 200:
            folder_files = files_in_folder.json()
            print(f"  found {folder_files['total']} files in folder")

        # ====================================================================
        # SCENARIO 6: Get File Details
        # ====================================================================
        print("\n--- SCENARIO 6: Get File Details ---")

        file_detail = requests.get(
            f"{BASE}/api/storage/student-files/files/{file1_id}",
            headers=h,
            timeout=30,
        )
        print(f"get file {file1_id}: {file_detail.status_code}")
        if file_detail.status_code != 200:
            print(f"  error: {file_detail.text}")
            return 1
        fdata = file_detail.json()
        print(f"  title={fdata['file_title']}")
        print(f"  folder_location={fdata.get('folder_location', 'Main')}")
        print(f"  storage_provider={fdata.get('storage_provider')}")

        # ====================================================================
        # SCENARIO 7: Update File Metadata
        # ====================================================================
        print("\n--- SCENARIO 7: Update File Metadata ---")

        update_resp = requests.patch(
            f"{BASE}/api/storage/student-files/files/{file1_id}",
            headers=hj,
            json={
                "file_title": "Assignment 1 - FINAL SUBMISSION",
                "description": "Updated description with final notes",
            },
            timeout=30,
        )
        print(f"update file {file1_id}: {update_resp.status_code}")
        if update_resp.status_code != 200:
            print(f"  error: {update_resp.text}")
            return 1
        updated_file = update_resp.json()
        print(f"  new title: {updated_file['file_title']}")

        # ====================================================================
        # SCENARIO 8: Move File to Different Folder
        # ====================================================================
        print("\n--- SCENARIO 8: Move File to Different Folder ---")

        move_resp = requests.patch(
            f"{BASE}/api/storage/student-files/files/{file2_id}",
            headers=hj,
            json={"folder_id": folder2_id},
            timeout=30,
        )
        print(f"move file {file2_id} to Projects folder: {move_resp.status_code}")
        if move_resp.status_code != 200:
            print(f"  error: {move_resp.text}")
            return 1

        # ====================================================================
        # SCENARIO 9: Update Folder Metadata
        # ====================================================================
        print("\n--- SCENARIO 9: Update Folder Metadata ---")

        update_folder = requests.patch(
            f"{BASE}/api/storage/student-files/folders/{folder1_id}",
            headers=hj,
            json={
                "folder_name": "Assignments (Updated)",
                "description": "Updated folder description",
            },
            timeout=30,
        )
        print(f"update folder {folder1_id}: {update_folder.status_code}")
        if update_folder.status_code != 200:
            print(f"  error: {update_folder.text}")
            return 1

        # ====================================================================
        # SCENARIO 10: Get Storage Usage
        # ====================================================================
        print("\n--- SCENARIO 10: Get Storage Usage ---")

        usage = requests.get(
            f"{BASE}/api/storage/student-files/usage",
            headers=h,
            timeout=30,
        )
        print(f"get storage usage: {usage.status_code}")
        if usage.status_code != 200:
            print(f"  error: {usage.text}")
            return 1
        usage_data = usage.json()
        print(f"  used_mb={usage_data['used_mb']}, quota_mb={usage_data['quota_mb']}")
        print(f"  remaining_mb={round(usage_data['remaining_bytes'] / (1024 * 1024), 2)}")
        print(f"  file_count={usage_data['file_count']}")

        # ====================================================================
        # SCENARIO 11: Delete File
        # ====================================================================
        print("\n--- SCENARIO 11: Delete File ---")

        delete_resp = requests.delete(
            f"{BASE}/api/storage/student-files/files/{file3_id}",
            headers=h,
            timeout=30,
        )
        print(f"delete file {file3_id}: {delete_resp.status_code}")
        if delete_resp.status_code not in (200, 204):
            print(f"  warning: {delete_resp.text}")

        # ====================================================================
        # SCENARIO 12: Bulk Delete
        # ====================================================================
        print("\n--- SCENARIO 12: Bulk Delete Files ---")

        # List files before bulk delete
        before_bulk = requests.get(
            f"{BASE}/api/storage/student-files/files?limit=500",
            headers=h,
            timeout=30,
        )
        file_ids_to_delete = [f["id"] for f in before_bulk.json().get("items", [])[:2]]

        if file_ids_to_delete:
            bulk_resp = requests.post(
                f"{BASE}/api/storage/student-files/bulk-delete",
                headers=hj,
                json={"ids": file_ids_to_delete, "resource_type": "file"},
                timeout=30,
            )
            print(f"bulk delete {len(file_ids_to_delete)} files: {bulk_resp.status_code}")

        # ====================================================================
        # FINAL: Storage Usage After Operations
        # ====================================================================
        print("\n--- FINAL: Storage Usage After Operations ---")

        final_usage = requests.get(
            f"{BASE}/api/storage/student-files/usage",
            headers=h,
            timeout=30,
        )
        if final_usage.status_code == 200:
            final_data = final_usage.json()
            print(f"  used_mb={final_data['used_mb']}, quota_mb={final_data['quota_mb']}")
            print(f"  file_count={final_data['file_count']}")

        print("\n✓ All scenarios completed successfully!")

        if args.teardown_after:
            print("\n--- CLEANUP ---")
            cleanup_student_files(student_token)
            print("✓ Cleanup completed")

        return 0

    except Exception as e:
        print(f"\n✗ Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
