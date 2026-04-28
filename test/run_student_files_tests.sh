#!/usr/bin/env bash
# Student file management API runner with explicit create/cleanup actions.
#
# Usage (from repo root that contains BE/, e.g. Source):
#   bash BE/test/run_student_files_tests.sh create
#   bash BE/test/run_student_files_tests.sh cleanup
#   bash BE/test/run_student_files_tests.sh create_and_cleanup
#

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${ROOT}/venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "venv not found at ${PY}; using python3" >&2
  PY=python3
fi
export API_BASE="${API_BASE:-http://127.0.0.1:8000}"

mode="${1:-create}"
if [[ $# -ge 1 ]]; then
  shift
fi

case "$mode" in
  create)
    exec "$PY" "${ROOT}/test/test_student_files_crud.py" "$@"
    ;;
  cleanup)
    exec "$PY" "${ROOT}/test/test_student_files_crud.py" --cleanup-only "$@"
    ;;
  create_and_cleanup)
    exec "$PY" "${ROOT}/test/test_student_files_crud.py" --teardown-after "$@"
    ;;
  *)
    echo "Usage: $0 [create|cleanup|create_and_cleanup]" >&2
    exit 1
    ;;
esac
