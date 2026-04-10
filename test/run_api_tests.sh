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
#   bash BE/test/run_api_tests.sh courses_delete_all
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
  courses_delete_all)
    exec bash "${ROOT}/test/run_courses_tests.sh" courses_delete_all "$@"
    ;;
  help|--help|-h)
    cat <<'EOF'
Usage:
  bash BE/test/run_api_tests.sh <action>

Actions:
  assignments_create
  assignments_cleanup
  assignments_create_and_cleanup
  courses_create
  courses_seed_create
  materials_create
  materials_cleanup
  materials_create_and_cleanup
  courses_delete_all
EOF
    ;;
  *)
    echo "Unknown action: $action" >&2
    echo "Run: bash BE/test/run_api_tests.sh --help" >&2
    exit 1
    ;;
esac

