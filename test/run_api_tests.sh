#!/usr/bin/env bash
# Unified API test runner with clear "create" vs "cleanup" commands.
#
# Usage (from repo root containing BE/):
#   bash BE/test/run_api_tests.sh assignments_create
#   bash BE/test/run_api_tests.sh assignments_cleanup
#   bash BE/test/run_api_tests.sh assignments_create_and_cleanup
#   bash BE/test/run_api_tests.sh courses_create
#   bash BE/test/run_api_tests.sh courses_seed_create
#   bash BE/test/run_api_tests.sh materials_create
#   bash BE/test/run_api_tests.sh materials_cleanup
#   bash BE/test/run_api_tests.sh materials_create_and_cleanup
#   bash BE/test/run_api_tests.sh course_permission_e2e
#   bash BE/test/run_api_tests.sh course_permission_cleanup
#   bash BE/test/run_api_tests.sh grading_create
#   bash BE/test/run_api_tests.sh grading_cleanup
#   bash BE/test/run_api_tests.sh grading_create_and_cleanup
#   bash BE/test/run_api_tests.sh courses_delete_all
#   bash BE/test/run_api_tests.sh student_files_create
#   bash BE/test/run_api_tests.sh student_files_cleanup
#   bash BE/test/run_api_tests.sh student_files_create_and_cleanup
#   bash BE/test/run_api_tests.sh notifications_create
#   bash BE/test/run_api_tests.sh notifications_cleanup
#   bash BE/test/run_api_tests.sh notifications_create_and_cleanup
#   bash BE/test/run_api_tests.sh notifications_create_keep   # leaves rows in Supabase
#   bash BE/test/run_api_tests.sh notifications_all_scenarios   # all scenario triggers, no cleanup
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

action="${1:-help}"
if [[ $# -ge 1 ]]; then
  shift
fi

case "$action" in
  assignments_create)
    exec bash "${ROOT}/test/run_assignments_tests.sh" create "$@"
    ;;
  assignments_cleanup)
    exec bash "${ROOT}/test/run_assignments_tests.sh" cleanup "$@"
    ;;
  assignments_create_and_cleanup)
    exec bash "${ROOT}/test/run_assignments_tests.sh" create_and_cleanup "$@"
    ;;
  grading_create)
    exec bash "${ROOT}/test/run_assignments_tests.sh" grading "$@"
    ;;
  grading_cleanup)
    exec bash "${ROOT}/test/run_assignments_tests.sh" grading_cleanup "$@"
    ;;
  grading_create_and_cleanup)
    exec bash "${ROOT}/test/run_assignments_tests.sh" grading_teardown "$@"
    ;;
  courses_create)
    exec bash "${ROOT}/test/run_courses_tests.sh" courses_create "$@"
    ;;
  courses_seed_create)
    exec bash "${ROOT}/test/run_courses_tests.sh" courses_seed_create "$@"
    ;;
  materials_create)
    exec bash "${ROOT}/test/run_courses_tests.sh" materials_create "$@"
    ;;
  materials_cleanup)
    exec bash "${ROOT}/test/run_courses_tests.sh" materials_cleanup "$@"
    ;;
  materials_create_and_cleanup)
    exec bash "${ROOT}/test/run_courses_tests.sh" materials_create_and_cleanup "$@"
    ;;
  course_permission_e2e)
    exec bash "${ROOT}/test/run_courses_tests.sh" course_permission_e2e "$@"
    ;;
  course_permission_cleanup)
    exec bash "${ROOT}/test/run_courses_tests.sh" course_permission_cleanup "$@"
    ;;
  courses_delete_all)
    exec bash "${ROOT}/test/run_courses_tests.sh" courses_delete_all "$@"
    ;;
  student_files_create)
    exec bash "${ROOT}/test/run_student_files_tests.sh" create "$@"
    ;;
  student_files_cleanup)
    exec bash "${ROOT}/test/run_student_files_tests.sh" cleanup "$@"
    ;;
  student_files_create_and_cleanup)
    exec bash "${ROOT}/test/run_student_files_tests.sh" create_and_cleanup "$@"
    ;;
  notifications_create)
    exec "${ROOT}/venv/bin/python" "${ROOT}/test/test_notifications_flow.py" "$@"
    ;;
  notifications_cleanup)
    exec "${ROOT}/venv/bin/python" "${ROOT}/test/test_notifications_flow.py" --cleanup-only "$@"
    ;;
  notifications_create_and_cleanup)
    exec "${ROOT}/venv/bin/python" "${ROOT}/test/test_notifications_flow.py" --start-cleanup --teardown-after "$@"
    ;;
  notifications_create_keep)
    exec "${ROOT}/venv/bin/python" "${ROOT}/test/test_notifications_flow.py" --keep-data "$@"
    ;;
  notifications_all_scenarios)
    exec "${ROOT}/venv/bin/python" "${ROOT}/test/test_notification_scenarios_e2e.py" "$@"
    ;;
  help|--help|-h)
    cat <<'EOF'
Usage:
  bash BE/test/run_api_tests.sh <action>

Actions:
  assignments_create
  assignments_cleanup
  assignments_create_and_cleanup
  grading_create
  grading_cleanup
  grading_create_and_cleanup
  courses_create
  courses_seed_create
  materials_create
  materials_cleanup
  materials_create_and_cleanup
  course_permission_e2e
  course_permission_cleanup
  courses_delete_all
  student_files_create
  student_files_cleanup
  student_files_create_and_cleanup
  notifications_create
  notifications_cleanup
  notifications_create_and_cleanup
  notifications_create_keep
  notifications_all_scenarios
EOF
    ;;
  *)
    echo "Unknown action: $action" >&2
    echo "Run: bash BE/test/run_api_tests.sh --help" >&2
    exit 1
    ;;
esac

