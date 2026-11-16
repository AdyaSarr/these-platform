"""The suite under pytest-xdist: the workers of one session share its lock, each
records the order of its tests, a failure prints how to replay a worker, and a test
marked serial refuses to run in a worker.

Child runs use another database name (on a port that is never open): their lock is
not the one of this session, and the tests they run never touch a database.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests import pytest_lock
from tests.pytest_child import BASE_DIR, FAKE_DATABASE, child_env, run_pytest

# Child pytest runs and timings: alone, after the parallel phase (scripts/test_all.sh).
pytestmark = pytest.mark.serial

PURE_TEST = "apps/demo/tests/test_demo_db.py::test_only_demo_names_are_accepted"  # 9 cases
SEED = "1234"


def _env(name: str) -> dict[str, str]:
    return child_env(DATABASE_URL=FAKE_DATABASE.format(name=name))


def _order_dir(name: str) -> Path:
    lock = pytest_lock.path_for({"NAME": name, "HOST": "127.0.0.1", "PORT": 1, "TEST": {}})
    return pytest_lock.order_dir(lock)


@pytest.fixture(scope="module")
def parallel_run() -> tuple[str, int, Path]:
    result = run_pytest(
        "-n", "2", "-v", "-p", "randomly", f"--randomly-seed={SEED}", PURE_TEST,
        env=_env("parallel_check"),
    )  # fmt: skip
    return result.stdout + result.stderr, result.returncode, _order_dir("parallel_check")


def test_the_workers_of_one_session_do_not_block_each_other(
    parallel_run: tuple[str, int, Path],
) -> None:
    output, code, _ = parallel_run
    assert code == 0, output[-3000:]
    assert "9 passed" in output
    # Both workers ran tests, hence both checked the lock of their controller.
    assert "[gw0]" in output and "[gw1]" in output


def test_the_seed_is_printed_at_the_end_under_xdist(parallel_run: tuple[str, int, Path]) -> None:
    output, _, _ = parallel_run
    # The end of the output is what a `tail` keeps (root conftest).
    assert f"pytest-randomly: replay with --randomly-seed={SEED}" in output.splitlines()[-3:]


def test_each_worker_records_the_order_of_its_tests(parallel_run: tuple[str, int, Path]) -> None:
    output, _, folder = parallel_run
    files = sorted(folder.glob("gw*.txt"))
    assert [f.name for f in files] == ["gw0.txt", "gw1.txt"], output[-2000:]
    lines = [line for f in files for line in f.read_text().splitlines()]
    assert len(lines) == len(set(lines)) == 9
    assert all(line.startswith(PURE_TEST + "[") for line in lines)


def test_the_order_of_a_worker_replays_in_the_same_order(
    parallel_run: tuple[str, int, Path],
) -> None:
    _, _, folder = parallel_run
    order = (folder / "gw0.txt").read_text().splitlines()
    assert order
    result = run_pytest(
        "-p", "no:xdist", "-v", f"--randomly-seed={SEED}", "--randomly-dont-reorganize",
        f"@{folder / 'gw0.txt'}",
        env=_env("parallel_check"),
    )  # fmt: skip
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-3000:]
    # Parameter ids may contain spaces ("x_demo; DROP"): match up to the closing bracket.
    ran = re.findall(r"^(\S+::[^\s\[]+(?:\[[^\]]*\])?) PASSED", output, flags=re.MULTILINE)
    assert ran == order


def test_a_serial_test_fails_in_a_worker_and_the_replay_is_printed() -> None:
    serial_test = "tests/test_pytest_lock.py::test_one_lock_per_test_database"
    result = run_pytest(
        "-n", "2", "-p", "randomly", f"--randomly-seed={SEED}", serial_test,
        env=_env("serial_check"),
    )  # fmt: skip
    output = result.stdout + result.stderr
    assert result.returncode == 1, output[-3000:]
    assert "serial" in output and "scripts/test_all.sh" in output
    assert "passed" not in output
    folder = _order_dir("serial_check").relative_to(BASE_DIR)
    assert re.search(
        rf"--randomly-dont-reorganize @{re.escape(str(folder))}/gw[01]\.txt", output
    ), output[-3000:]


def test_a_session_without_workers_keeps_the_orders_of_the_last_parallel_one(
    parallel_run: tuple[str, int, Path],
) -> None:
    """scripts/test_all.sh runs its serial phase after the parallel one: the replay
    printed by the parallel phase must still find its files."""
    _, _, folder = parallel_run
    before = {f.name: f.read_text() for f in folder.glob("gw*.txt")}
    assert before
    result = run_pytest(
        "-p", "no:xdist", "-p", "no:randomly", f"{PURE_TEST}[theses]",
        env=_env("parallel_check"),
    )  # fmt: skip
    assert result.returncode == 0, (result.stdout + result.stderr)[-2000:]
    assert {f.name: f.read_text() for f in folder.glob("gw*.txt")} == before
