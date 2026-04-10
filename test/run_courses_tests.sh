#!/usr/bin/env bash
# Course/material API runner with explicit create/cleanup actions.
#
# Usage (from repo root that contains BE/, e.g. Source):
#   bash BE/test/run_courses_tests.sh courses_create
#   bash BE/test/run_courses_tests.sh courses_seed_create
#   bash BE/test/run_courses_tests.sh materials_create
#   bash BE/test/run_courses_tests.sh materials_cleanup
#   bash BE/test/run_courses_tests.sh materials_create_and_cleanup
#   bash BE/test/run_courses_tests.sh courses_delete_all
#
# Legacy aliases still supported: crud, seed, materials, materials_teardown, delete_all, all

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${ROOT}/venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "venv not found at ${PY}; using python3" >&2
  PY=python3
fi
export API_BASE="${API_BASE:-http://127.0.0.1:8000}"

mode="${1:-all}"
if [[ $# -ge 1 ]]; then
  shift
fi
case "$mode" in
  courses_create|crud)
    exec "$PY" "${ROOT}/test/test_courses_crud.py" "$@"
    ;;
  courses_seed_create|seed)
    exec "$PY" "${ROOT}/test/test_courses_seed.py" "$@"
    ;;
  materials_create|materials)
    exec "$PY" "${ROOT}/test/test_module_materials_crud.py" "$@"
    ;;
  materials_cleanup)
    exec "$PY" "${ROOT}/test/test_module_materials_crud.py" --cleanup-only "$@"
    ;;
  materials_create_and_cleanup|materials_teardown)
    exec "$PY" "${ROOT}/test/test_module_materials_crud.py" --teardown-after "$@"
    ;;
  courses_delete_all|delete_all)
    exec "$PY" "${ROOT}/test/test_courses_delete_all.py" --yes "$@"
    ;;
  all)
    "$PY" "${ROOT}/test/test_courses_crud.py"
    "$PY" "${ROOT}/test/test_courses_seed.py"
    ;;
  *)
    echo "Usage: $0 [courses_create|courses_seed_create|materials_create|materials_cleanup|materials_create_and_cleanup|courses_delete_all|all]" >&2
    exit 1
    ;;
esac
