#!/usr/bin/env bash
# The full test suite, in two phases with one seed (CLAUDE.md, docs/decisions.md):
#   1. in parallel (pytest-xdist): every test not marked serial;
#   2. alone: the tests marked serial (threads, timeouts, child pytest runs).
# merge_gate tests stay out: `uv run pytest -m merge_gate`.
#
#   scripts/test_all.sh [pytest arguments...] > var/last-pytest.log 2>&1; echo "pytest exit=$?"
#
# PYTEST_WORKERS  number of workers of phase 1 (default 4: measured on a 10-core Mac,
#                 no gain beyond, see docs/decisions.md)
# PYTEST_SEED     pytest-randomly seed of both phases (default: drawn at random)
#
# Exit code: the first non-zero code of the two phases (both always run), else 0.
# Last line: test_all: exit=N parallel=N serial=N seed=S
#
# With pytest arguments (paths, -k...), a phase that selects no test (pytest exit 5)
# counts as 0. Without, it means lost tests: it is a failure.

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO" || exit 2

workers="${PYTEST_WORKERS:-4}"
if [[ ! "$workers" =~ ^[1-9][0-9]?$ ]]; then
  echo "test_all: PYTEST_WORKERS must be a number from 1 to 99, not '$workers'" >&2
  exit 2
fi

if [[ -n "${PYTEST_SEED+set}" ]]; then
  seed="$PYTEST_SEED"
else
  seed=$(( (RANDOM << 15) | RANDOM ))
fi
if [[ ! "$seed" =~ ^[0-9]{1,10}$ ]]; then
  echo "test_all: PYTEST_SEED must be a non-negative integer, not '$seed'" >&2
  exit 2
fi

echo "test_all: workers=$workers seed=$seed"

echo "test_all: phase 1/2, parallel"
uv run pytest -q -n "$workers" --dist load --max-worker-restart=0 \
  --randomly-seed="$seed" -m "not serial and not merge_gate" "$@"
parallel=$?

echo "test_all: phase 2/2, serial"
uv run pytest -q -p no:xdist \
  --randomly-seed="$seed" -m "serial and not merge_gate" "$@"
serial=$?

if [[ $# -gt 0 ]]; then
  [[ $parallel -eq 5 && $serial -ne 5 ]] && parallel_ok=0 || parallel_ok=$parallel
  [[ $serial -eq 5 && $parallel -ne 5 ]] && serial_ok=0 || serial_ok=$serial
else
  parallel_ok=$parallel
  serial_ok=$serial
fi

code=0
if [[ $parallel_ok -ne 0 ]]; then
  code=$parallel_ok
elif [[ $serial_ok -ne 0 ]]; then
  code=$serial_ok
fi

echo "test_all: exit=$code parallel=$parallel serial=$serial seed=$seed"
exit "$code"
