"""scripts/test_all.sh: the full suite in two phases (parallel, then serial), one seed,
and the real exit code of both.

The script is run with a fake `uv` first on PATH, which records its arguments and
exits with the codes it is told, one per call: no pytest is started.
"""

from __future__ import annotations

import re
import shlex
import stat
import subprocess  # nosec B404
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "test_all.sh"
BASH = "/bin/bash"


def _run(
    tmp_path: Path, *args: str, codes: str = "0 0", **env: str
) -> tuple[int, str, list[list[str]]]:
    calls = tmp_path / "calls.txt"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    counter = tmp_path / "count"
    counter.write_text("0")
    fake = bin_dir / "uv"
    fake.write_text(
        "#!/bin/bash\n"
        f'printf "%q " "$@" >> "{calls}"; echo >> "{calls}"\n'
        f'n=$(cat "{counter}"); echo $((n + 1)) > "{counter}"\n'
        f"codes=({codes})\n"
        'exit "${codes[$n]:-0}"\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    result = subprocess.run(  # nosec B603
        [BASH, str(SCRIPT), *args],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    lines = calls.read_text().splitlines() if calls.exists() else []
    return result.returncode, result.stdout + result.stderr, [shlex.split(c) for c in lines]


def _option(call: list[str], name: str) -> str:
    for i, arg in enumerate(call):
        if arg == name:
            return call[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    raise AssertionError(f"{name} not in {call}")


def test_the_script_is_valid_bash() -> None:
    result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_two_complementary_phases_with_one_seed(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path)
    assert code == 0, output
    assert len(calls) == 2
    parallel, serial = calls
    assert parallel[:3] == ["run", "pytest", "-q"] and serial[:3] == ["run", "pytest", "-q"]
    assert _option(parallel, "-m") == "not serial and not merge_gate"
    assert _option(serial, "-m") == "serial and not merge_gate"
    assert _option(parallel, "-n") == "6"
    assert _option(parallel, "--dist") == "load"
    assert _option(parallel, "--max-worker-restart") == "0"
    assert _option(serial, "-p") == "no:xdist"
    seed = _option(parallel, "--randomly-seed")
    assert re.fullmatch(r"[0-9]+", seed) and _option(serial, "--randomly-seed") == seed
    assert output.rstrip().splitlines()[-1] == f"test_all: exit=0 parallel=0 serial=0 seed={seed}"


def test_workers_and_seed_come_from_the_environment(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, PYTEST_WORKERS="8", PYTEST_SEED="42")
    assert code == 0, output
    assert _option(calls[0], "-n") == "8"
    assert {_option(c, "--randomly-seed") for c in calls} == {"42"}


@pytest.mark.parametrize(
    ("variable", "value"),
    [("PYTEST_WORKERS", "0"), ("PYTEST_WORKERS", "auto"), ("PYTEST_WORKERS", "6;rm"),
     ("PYTEST_SEED", "-1"), ("PYTEST_SEED", "abc"), ("PYTEST_SEED", "")],
)  # fmt: skip
def test_bad_settings_are_refused_before_any_run(tmp_path: Path, variable: str, value: str) -> None:
    code, output, calls = _run(tmp_path, **{variable: value})
    assert code == 2, output
    assert variable in output
    assert calls == []


@pytest.mark.parametrize(
    ("codes", "expected"),
    [("1 0", 1), ("0 1", 1), ("3 0", 3), ("0 3", 3), ("1 2", 1), ("5 0", 5), ("0 5", 5)],
)
def test_the_exit_code_is_the_first_failure_and_both_phases_run(
    tmp_path: Path, codes: str, expected: int
) -> None:
    code, output, calls = _run(tmp_path, codes=codes)
    assert code == expected, output
    assert len(calls) == 2  # the serial phase runs even after a failed parallel phase
    first, second = codes.split()
    assert f"test_all: exit={expected} parallel={first} serial={second} " in output


def test_extra_arguments_go_to_both_phases(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, "-x", "--durations=30")
    assert code == 0, output
    assert all(c[-2:] == ["-x", "--durations=30"] for c in calls)


def test_with_extra_arguments_a_phase_without_tests_is_not_a_failure(tmp_path: Path) -> None:
    """`scripts/test_all.sh apps/travel` may select no serial test (exit 5): fine. In
    the full suite (no argument) an empty phase means lost tests: a failure (above)."""
    code, output, _ = _run(tmp_path, "apps/travel", codes="0 5")
    assert code == 0, output
    code, output, _ = _run(tmp_path, "apps/travel", codes="5 5")
    assert code == 5, output
