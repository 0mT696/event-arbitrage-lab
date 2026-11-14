#!/usr/bin/env bash
# Local quality gate — replaces CI until GitHub Actions is wanted again.
# Run from repo root: bash scripts/check.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== pytest =="
python3 -m pytest -q

if command -v ruff >/dev/null 2>&1; then
  echo "== ruff =="
  ruff check src tests
else
  echo "== ruff skipped (not installed) =="
fi

echo "GATE PASSED"
