#!/usr/bin/env bash
# Replays the mutants of scripts/mutants.toml (SR-12), by hand only: never from
# scripts/test_all.sh. Refuses to start on a dirty tree; always restores the
# mutated file, on exit, Ctrl-C or kill (and a next run restores a leftover).
#
#   scripts/mutants.sh [--list] [Mnn ...] > var/mutants.log 2>&1; echo "mutants exit=$?"
#
# Exit code: 0 every mutant killed, 1 a survivor or an error, 2 not started
# (dirty tree, red baseline, unknown id), 130 interrupted.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO" || exit 2

if [[ -n "${DJANGO_SETTINGS_MODULE+set}" && "$DJANGO_SETTINGS_MODULE" != "config.settings.test" ]]; then
  echo "mutants: DJANGO_SETTINGS_MODULE='$DJANGO_SETTINGS_MODULE' est exportée ; unset puis relancez." >&2
  exit 2
fi

trap 'uv run --quiet python scripts/mutants.py --restore-only' EXIT
trap 'exit 130' INT TERM

uv run --quiet python scripts/mutants.py "$@"
status=$?
exit "$status"
