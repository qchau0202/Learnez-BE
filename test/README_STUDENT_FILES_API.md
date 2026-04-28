# Student File Management API - Testing Guide

## Overview

Comprehensive end-to-end tests for student file management APIs including:
- Folder CRUD operations (create, list, get, update, delete)
- File CRUD operations (upload, list, get, update, delete)
- Storage quota tracking
- Nested folder structure
- File moving between folders
- Bulk operations

## Quick Start

### Run from repo root (where `BE/` folder exists)

```bash
# Test scenarios (keeps data for inspection)
bash BE/test/run_api_tests.sh student_files_create

# Cleanup only (removes all E2E test files/folders)
bash BE/test/run_api_tests.sh student_files_cleanup

# Run scenarios then cleanup immediately
bash BE/test/run_api_tests.sh student_files_create_and_cleanup
```

### Or run directly

```bash
# From repo root
BE/venv/bin/python BE/test/test_student_files_crud.py

# With cleanup after
BE/venv/bin/python BE/test/test_student_files_crud.py --teardown-after

# Cleanup only
BE/venv/bin/python BE/test/test_student_files_crud.py --cleanup-only
```

## Prerequisites

1. **Backend running**:
   ```bash
   cd BE
   python main.py
   # Or: uvicorn app.main:app --reload
   ```

2. **Environment variables** (optional):
   ```bash
   export API_BASE="http://127.0.0.1:8000"  # default
   export ADMIN_EMAIL="learnez@email.com"    # default
   export ADMIN_PASSWORD="123456"            # default
   export STUDENT_EMAIL="student@example.com" # optional
   export STUDENT_PASSWORD="123456"          # default
   ```

3. **Database** - Supabase PostgreSQL tables created:
   ```bash
   # Run SQL migration in Supabase console
   # See: BE/sql/001_student_files_schema.sql
   ```

4. **Cloudinary** - Configured in environment:
   ```bash
   export CLOUDINARY_CLOUD_NAME="your_cloud_name"
   export CLOUDINARY_API_KEY="your_api_key"
   export CLOUDINARY_API_SECRET="your_api_secret"
   ```

## Test Scenarios

The test suite (`test_student_files_crud.py`) runs 12 comprehensive scenarios:

### 1. Create Folders
- Create main folder: "Assignments"
- Create main folder: "Projects"
- Create nested folder: "Week 1" (under Assignments)

**Expected**: 201 Created, folder IDs returned

### 2. List Folders
- List all main folders
- List sub-folders of specific parent

**Expected**: 200 OK, folder list with total count

### 3. Get Folder Details
- Retrieve single folder
- Includes file count

**Expected**: 200 OK, folder data with file_count

### 4. Upload Files
- File 1: Upload to "Assignments" folder
- File 2: Upload to nested "Week 1" folder
- File 3: Upload to Main folder (no parent)

**Expected**: 201 Created, file metadata with Cloudinary URL

### 5. List Files
- List all files with pagination
- List files in specific folder

**Expected**: 200 OK, file list with storage usage

### 6. Get File Details
- Retrieve single file
- Includes folder location breadcrumb

**Expected**: 200 OK, file data with folder_location

### 7. Update File Metadata
- Change title and description of file
- Keep Cloudinary reference unchanged

**Expected**: 200 OK, updated file data

### 8. Move File to Different Folder
- Move file from "Week 1" to "Projects" folder

**Expected**: 200 OK, file with new folder_id

### 9. Update Folder Metadata
- Change folder name and description

**Expected**: 200 OK, updated folder data

### 10. Get Storage Usage
- Check current storage used
- Show quota and remaining space
- Display file count

**Expected**: 200 OK, usage data in MB and bytes

### 11. Delete File
- Soft-delete single file
- Removes from Cloudinary

**Expected**: 204 No Content

### 12. Bulk Delete
- Delete multiple files at once

**Expected**: 204 No Content

## Test Output Example

```
✓ Logged in as student: student123@email.com

--- SCENARIO 1: Create Folders ---
create folder 'Assignments': 201
  id=1, name=Assignments
create folder 'Projects': 201
  id=2, name=Projects
create nested folder 'Week 1' (under Assignments): 201
  id=3, name=Week 1

--- SCENARIO 2: List Folders ---
list folders: 200
  found 2 main folders
    - Assignments (id=1)
    - Projects (id=2)
list sub-folders of Assignments: 200
  found 1 sub-folders

--- SCENARIO 3: Get Folder Details ---
get folder 1: 200
  name=Assignments, file_count=1

--- SCENARIO 4: Upload Files ---
upload file1 to folder 1: 201
  id=42, title=Assignment 1, size=44 bytes
upload file2 to nested folder 3: 201
  id=43, title=Week 1 Solutions, size=65 bytes
upload file3 to Main folder: 201
  id=44, title=README, size=31 bytes

... (more scenarios)

--- FINAL: Storage Usage After Operations ---
  used_mb=0.00, quota_mb=10.0
  file_count=1

✓ All scenarios completed successfully!
```

## Error Cases Tested

The implementation handles:

- ✅ Invalid file types (e.g., `.exe`)
- ✅ File too large (exceeds 5MB per file)
- ✅ Storage quota exceeded (exceeds 10MB per student)
- ✅ Folder not found
- ✅ File not found
- ✅ Folder not empty (when deleting)
- ✅ Unauthorized access (other student's files)
- ✅ Circular folder references (folder as own parent)

## Cleanup Strategy

### Default Behavior
Tests keep all created data for inspection:
- Folders remain in database
- Files remain soft-deleted but recoverable
- Useful for debugging failures

### Cleanup Options

**After test completes normally**:
```bash
BE/venv/bin/python BE/test/test_student_files_crud.py --teardown-after
```

**Clean up without running tests**:
```bash
BE/venv/bin/python BE/test/test_student_files_crud.py --cleanup-only
```

**From test runner**:
```bash
bash BE/test/run_api_tests.sh student_files_cleanup
bash BE/test/run_api_tests.sh student_files_create_and_cleanup
```

## Debugging Failed Tests

1. **Check API is running**:
   ```bash
   curl http://127.0.0.1:8000/api/iam/login -X POST -H "Content-Type: application/json" \
     -d '{"email":"learnez@email.com","password":"123456"}'
   ```

2. **Check Cloudinary configuration**:
   ```bash
   echo $CLOUDINARY_CLOUD_NAME $CLOUDINARY_API_KEY
   ```

3. **Check database tables exist**:
   ```sql
   SELECT table_name FROM information_schema.tables 
   WHERE table_schema = 'public' 
   AND table_name IN ('student_folders', 'student_files');
   ```

4. **View detailed error output**:
   ```bash
   BE/venv/bin/python BE/test/test_student_files_crud.py 2>&1 | tee test_output.log
   ```

5. **Test specific endpoint manually**:
   ```bash
   # Get token
   TOKEN=$(curl -s http://127.0.0.1:8000/api/iam/login -X POST \
     -H "Content-Type: application/json" \
     -d '{"email":"learnez@email.com","password":"123456"}' | jq -r .access_token)
   
   # List folders
   curl -H "Authorization: Bearer $TOKEN" \
     http://127.0.0.1:8000/api/storage/student-files/folders
   ```

## Common Issues

### `403 Forbidden`
- Student trying to access another student's files
- Check `student_id` matches token owner

### `413 Payload Too Large`
- File exceeds 5MB limit (configurable)
- Reduce file size or adjust `STUDENT_FILE_MAX_MB`

### `409 Conflict`
- Folder already exists with that name
- Delete or rename the existing folder

### `500 Cloudinary Upload Failed`
- Credentials not configured
- Check `CLOUDINARY_*` env variables
- Verify Cloudinary account has API access

### Timeout (30s)
- API not responding
- Check if backend is running
- Check network connectivity

## Test File Structure

```
BE/test/
├── test_student_files_crud.py      # Main E2E test scenarios
├── run_student_files_tests.sh       # Test runner wrapper
├── run_api_tests.sh                 # Master test orchestrator
└── README_STUDENT_FILES_API.md      # This file
```

## Extending Tests

To add more scenarios, edit `test_student_files_crud.py`:

```python
# Add new scenario section
print("\n--- SCENARIO 13: Your New Test ---")

# Use existing patterns
response = requests.post(
    f"{BASE}/api/storage/student-files/folders",
    headers=hj,
    json={"folder_name": "New Folder"},
    timeout=30,
)
print(f"your test: {response.status_code}")
if response.status_code != 201:
    print(f"  error: {response.text}")
    return 1
```

## Performance Notes

- ⏱️ Full test suite: ~10-15 seconds
- 📊 Creates ~3 folders, ~3 files
- 💾 Total storage used: <1MB
- 🔄 Supports multiple test runs on same student (soft deletes)

## Integration with CI/CD

```yaml
# Example GitHub Actions
- name: Test Student Files API
  run: |
    bash BE/test/run_api_tests.sh student_files_create_and_cleanup
  env:
    API_BASE: http://localhost:8000
    ADMIN_EMAIL: ci@test.local
    ADMIN_PASSWORD: ${{ secrets.ADMIN_PASSWORD }}
```

## References

- **API Documentation**: [BE/docs/STUDENT_FILES_API.md](../docs/STUDENT_FILES_API.md)
- **Schema**: [BE/sql/001_student_files_schema.sql](../sql/001_student_files_schema.sql)
- **Models**: [BE/app/models/student_files.py](../app/models/student_files.py)
- **Service**: [BE/app/services/storage/student_files_db.py](../app/services/storage/student_files_db.py)
- **Routes**: [BE/app/api/storage/student_files_routes.py](../app/api/storage/student_files_routes.py)
