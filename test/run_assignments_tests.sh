#!/usr/bin/env bash
# Assignment API test runner (explicit create/cleanup split).
#
# Usage (from repo root, e.g. Source):
#   bash BE/test/run_assignments_tests.sh create
#   bash BE/test/run_assignments_tests.sh grading
#   bash BE/test/run_assignments_tests.sh cleanup
#   bash BE/test/run_assignments_tests.sh create_and_cleanup
#
# Optional passthrough flags:
#   bash BE/test/run_assignments_tests.sh create --start-cleanup
#   bash BE/test/run_assignments_tests.sh create --teardown-after
#
# Legacy aliases kept for compatibility:
#   test -> create, teardown -> create_and_cleanup
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${ROOT}/venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "venv not found at ${PY}; using python3" >&2
  PY=python3
fi
export API_BASE="${API_BASE:-http://127.0.0.1:8000}"

mode=create
if [[ $# -ge 1 && "$1" != -* ]]; then
  case "$1" in
    create|grading|cleanup|create_and_cleanup|test|teardown)
      mode=$1
      shift
      ;;
  esac
fi

case "$mode" in
  create|test)
    exec "$PY" "${ROOT}/test/test_assignments_crud.py" "$@"
    ;;
  grading)
    exec "$PY" "${ROOT}/test/test_grading_flow.py" "$@"
    ;;
  cleanup)
    exec "$PY" "${ROOT}/test/test_assignments_crud.py" --cleanup-only "$@"
    ;;
  create_and_cleanup|teardown)
    exec "$PY" "${ROOT}/test/test_assignments_crud.py" --teardown-after "$@"
    ;;
  *)
    echo "Usage: $0 [create|grading|cleanup|create_and_cleanup] [python args...]" >&2
    exit 1
    ;;
esac
