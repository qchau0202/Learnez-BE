#!/usr/bin/env bash
# Run course API tests using BE virtualenv Python.
# Usage (from repo root that contains BE/, e.g. Source):
#   bash BE/test/run_courses_tests.sh [crud|seed|delete_all|all]
# Or from BE/: bash test/run_courses_tests.sh ...
# Default: all  → crud + seed (no delete)

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${ROOT}/venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "venv not found at ${PY}; using python3" >&2
  PY=python3
fi
export API_BASE="${API_BASE:-http://127.0.0.1:8000}"

mode="${1:-all}"
case "$mode" in
  crud)       exec "$PY" "${ROOT}/test/test_courses_crud.py" ;;
  seed)       exec "$PY" "${ROOT}/test/test_courses_seed.py" ;;
  delete_all) exec "$PY" "${ROOT}/test/test_courses_delete_all.py" --yes ;;
  all)
    "$PY" "${ROOT}/test/test_courses_crud.py"
    "$PY" "${ROOT}/test/test_courses_seed.py"
    ;;
  *) echo "Usage: $0 [crud|seed|delete_all|all]" >&2; exit 1 ;;
esac
