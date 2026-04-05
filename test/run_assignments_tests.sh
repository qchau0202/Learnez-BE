#!/usr/bin/env bash
# Run assignment E2E from BE/ (scripts live in BE/test/).
# Usage (from repo root that contains BE/, e.g. Source):
#   bash BE/test/run_assignments_tests.sh                 # scenarios only; keeps E2E course in DB
#   bash BE/test/run_assignments_tests.sh test            # same
#   bash BE/test/run_assignments_tests.sh cleanup         # only delete E2E-ASGN-SCENARIOS (no scenarios)
#   bash BE/test/run_assignments_tests.sh teardown        # scenarios then delete course/module
# Flags can follow the mode or be used without a mode if they start with -:
#   bash BE/test/run_assignments_tests.sh test --skip-start-cleanup
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${ROOT}/venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "venv not found at ${PY}; using python3" >&2
  PY=python3
fi
export API_BASE="${API_BASE:-http://127.0.0.1:8000}"

mode=test
if [[ $# -ge 1 && "$1" != -* ]]; then
  case "$1" in
    test|cleanup|teardown)
      mode=$1
      shift
      ;;
  esac
fi

case "$mode" in
  test)
    exec "$PY" "${ROOT}/test/test_assignments_crud.py" "$@"
    ;;
  cleanup)
    exec "$PY" "${ROOT}/test/test_assignments_crud.py" --cleanup-only "$@"
    ;;
  teardown)
    exec "$PY" "${ROOT}/test/test_assignments_crud.py" --teardown-after "$@"
    ;;
  *)
    echo "Usage: $0 [test|cleanup|teardown] [python args...]" >&2
    exit 1
    ;;
esac
