# API Test Guide (Create vs Cleanup)

This folder now uses an explicit **create** vs **cleanup** split so developers can run tests safely and predictably.

## Quick start

Run all commands from the repo root (the folder that contains `BE/`).

```bash
# Show all available actions
bash BE/test/run_api_tests.sh --help
```

## Copy-paste commands by purpose

### 1) Assignments API

```bash
# Create/test scenarios only (keeps data)
bash BE/test/run_api_tests.sh assignments_create

# Cleanup only (removes E2E assignment test data)
bash BE/test/run_api_tests.sh assignments_cleanup

# Create scenarios then cleanup immediately
bash BE/test/run_api_tests.sh assignments_create_and_cleanup
```

### 2) Grading flow (MCQ auto + manual essay)

Uses course code `E2E-GRADING-FLOW` and `/api/grading/...` endpoints.

```bash
# Run flow only (keeps data). If course_code already exists, add --start-cleanup:
bash BE/test/run_api_tests.sh grading_create
bash BE/test/run_api_tests.sh grading_create --start-cleanup

# Cleanup only (no test run)
bash BE/test/run_api_tests.sh grading_cleanup

# Run flow then delete E2E course/module
bash BE/test/run_api_tests.sh grading_create_and_cleanup
```

### 3) Courses API

```bash
# CRUD create/list/get/update flow (keeps created course)
bash BE/test/run_api_tests.sh courses_create

# Seed demo courses (keeps data)
bash BE/test/run_api_tests.sh courses_seed_create

# Delete ALL courses (destructive; uses --yes internally)
bash BE/test/run_api_tests.sh courses_delete_all
```

### 4) Module Materials API

```bash
# Upload/update/delete scenario run (keeps E2E course/module by default)
bash BE/test/run_api_tests.sh materials_create

# Cleanup only (removes E2E module materials test data)
bash BE/test/run_api_tests.sh materials_cleanup

# Run scenario then cleanup immediately
bash BE/test/run_api_tests.sh materials_create_and_cleanup

# Optional: include material deletion in scenario itself
bash BE/test/run_api_tests.sh materials_create --delete-material-after
```

## Direct runner scripts (if needed)

You can also call domain runners directly:

```bash
bash BE/test/run_assignments_tests.sh create
bash BE/test/run_assignments_tests.sh cleanup
bash BE/test/run_assignments_tests.sh grading
bash BE/test/run_assignments_tests.sh grading_cleanup
bash BE/test/run_courses_tests.sh materials_cleanup
```

## Environment variables

Optional environment variables for API and auth:

```bash
export API_BASE=http://127.0.0.1:8000
export ADMIN_EMAIL=learnez@email.com
export ADMIN_PASSWORD=123456
```

Assignment/material tests also support lecturer/student credentials via env vars defined in their Python files.

## Notes

- `*_cleanup` commands are intentionally separate and **only perform deletion**.
- `*_create` commands are intentionally separate and **do not auto-delete** unless you use `*_create_and_cleanup`.
- `*_create` commands also **do not perform start-cleanup** by default.
- If needed, use explicit pre-cleanup flags:
  - `bash BE/test/run_assignments_tests.sh create --start-cleanup`
  - `bash BE/test/run_courses_tests.sh materials_create --start-cleanup`
- For safety, `courses_delete_all` is destructive and should be used only in test/dev environments.

