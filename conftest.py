"""Project-wide pytest configuration.

Fails the whole run early, with an actionable message, when PostgreSQL is not
reachable (tests run on PostgreSQL only, see docs/decisions.md), or when another
pytest run already uses the test database (tests/pytest_lock.py).

Under pytest-xdist (scripts/test_all.sh): the controller holds the lock, the workers
check it, record the order of their tests for a replay, and refuse the tests marked
serial (they run alone, in the second phase of the script).
"""

import datetime as dt
import os
from collections.abc import Generator
from pathlib import Path
from typing import IO, Any

import pytest
import time_machine
from django.db import OperationalError, connections
from pytest_django import DjangoDbBlocker

from tests import pytest_lock

DB_UNREACHABLE_MESSAGE = (
    "PostgreSQL est injoignable (DATABASE_URL). "
    "Lancez `docker compose up -d db` puis relancez pytest."
)


SERIAL_MESSAGE = (
    "Test marqué serial lancé dans un worker xdist : il doit tourner seul. "
    "Lancez la suite avec scripts/test_all.sh (phase série), ou ce test sans -n."
)
WORKER_LOCK_MESSAGE = "Worker xdist : le verrou de la session n'est pas tenu par son contrôleur"
NO_SESSION_LOCK_MESSAGE = "Le verrou de la session n'a pas été pris (pytest_configure)."
FALLBACK_DATABASE_MESSAGE = (
    "Base de test : serveur de DATABASE_URL (TEST_DATABASE_URL absente). Plus rapide : "
    "docker compose up -d db-test, puis TEST_DATABASE_URL dans .env (README)."
)

_LOCK_FD = pytest.StashKey[int]()
_ORDER_FILE = pytest.StashKey[IO[str]]()
# In the controller: the workers that ran a failing test, for the replay.
_failed_workers: set[str] = set()

# Options with which pytest runs no test: no lock is taken, so they stay usable
# while a run is in progress.
_NO_RUN_OPTIONS = ("help", "version", "collectonly", "showfixtures", "show_fixtures_per_test",
                   "markers")  # fmt: skip


def _is_worker(config: pytest.Config) -> bool:
    # config.workerinput, never the PYTEST_XDIST_* variables: a child pytest started
    # by a test inherits them without being a worker.
    return hasattr(config, "workerinput")


def pytest_configure(config: pytest.Config) -> None:
    """The lock of the session (tests/pytest_lock.py), taken by the process that owns
    the session: the xdist controller, or the only process without xdist. Taken here,
    before xdist starts any worker and before any test database is created."""
    if _is_worker(config):
        _start_worker(config)
        return
    if any(getattr(config.option, name, False) for name in _NO_RUN_OPTIONS):
        return
    from django.conf import settings

    path = pytest_lock.path_for(settings.DATABASES["default"])
    try:
        fd = pytest_lock.acquire(path)
    except pytest_lock.LockHeldError:
        pytest.exit(pytest_lock.MESSAGE, returncode=3)
    config.stash[_LOCK_FD] = fd
    pytest_lock.set_session_lock(path)
    # A session with workers replaces the orders of the previous one (which may have
    # had more workers); a session without keeps them: the serial phase of
    # scripts/test_all.sh must not delete the files the parallel phase points to.
    if getattr(config.option, "numprocesses", None):
        for stale in pytest_lock.order_dir(path).glob("gw*.txt"):
            stale.unlink()


def _start_worker(config: pytest.Config) -> None:
    held = config.workerinput.get("pytest_lock")  # type: ignore[attr-defined]
    if held is None:  # the controller runs no test (e.g. --collect-only)
        return
    path = Path(held)
    pytest_lock.set_session_lock(path)
    folder = pytest_lock.order_dir(path)
    folder.mkdir(parents=True, exist_ok=True)
    worker = config.workerinput["workerid"]  # type: ignore[attr-defined]
    config.stash[_ORDER_FILE] = (folder / f"{worker}.txt").open("w", buffering=1)


def pytest_unconfigure(config: pytest.Config) -> None:
    order = config.stash.get(_ORDER_FILE, None)
    if order is not None:
        order.close()
    fd = config.stash.get(_LOCK_FD, None)
    if fd is not None:
        del config.stash[_LOCK_FD]
        pytest_lock.set_session_lock(None)
        pytest_lock.release(fd)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:
    """xdist, in the controller, for each worker: which lock it relies on, and who
    holds it."""
    path = pytest_lock.session_lock()
    if path is not None:
        node.workerinput["pytest_lock"] = str(path)
        node.workerinput["pytest_lock_holder"] = os.getpid()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """In a worker: record the test in the order file, then refuse a serial test
    before any fixture (its database included) is set up: an omission of the
    script cannot pass for a green test."""
    if not _is_worker(item.config):
        return
    order = item.config.stash.get(_ORDER_FILE, None)
    if order is not None:
        order.write(item.nodeid + "\n")
    if item.get_closest_marker("serial"):
        pytest.fail(SERIAL_MESSAGE, pytrace=False)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    node = getattr(report, "node", None)  # set by xdist on the reports of a worker
    if report.failed and node is not None:
        _failed_workers.add(node.gateway.id)


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    """Print pytest-randomly's seed at the END, in every mode, and under xdist how to
    replay, alone and in the same order, each worker that ran a failing test.

    Its own header line is hidden by -q, and the end of the output is what a
    `tail` keeps: without this, a run failing at random could not be replayed.
    """
    from django.conf import settings

    # config/settings/test.py: said here, never when the settings are imported.
    if settings.configured and getattr(settings, "TEST_DATABASE_SOURCE", None) == "DATABASE_URL":
        terminalreporter.write_line(FALLBACK_DATABASE_MESSAGE)
    seed = getattr(config.option, "randomly_seed", None)
    path = pytest_lock.session_lock()
    if isinstance(seed, int) and path is not None:
        folder = pytest_lock.order_dir(path).relative_to(pytest_lock.VAR_DIR.parent)
        for worker in sorted(_failed_workers):
            terminalreporter.write_line(
                f"pytest-randomly: replay {worker} with: uv run pytest -p no:xdist "
                f"--randomly-seed={seed} --randomly-dont-reorganize @{folder}/{worker}.txt"
            )
    if isinstance(seed, int):
        terminalreporter.write_line(f"pytest-randomly: replay with --randomly-seed={seed}")


@pytest.fixture(scope="session", autouse=True)
def _pytest_lock(request: pytest.FixtureRequest) -> None:
    """The lock of the session is held (pytest_configure). In a worker: checked to be
    held by its controller, the parent process, and to be the lock of the database
    this worker derives its own from.
    django_db_modify_db_settings depends on it: the check precedes the creation of
    the test database, whatever order pytest picks for autouse fixtures."""
    config = request.config
    if not _is_worker(config):
        if pytest_lock.session_lock() is None:  # taken in pytest_configure
            pytest.exit(NO_SESSION_LOCK_MESSAGE, returncode=3)
        return
    from django.conf import settings

    held = config.workerinput.get("pytest_lock")  # type: ignore[attr-defined]
    holder = config.workerinput.get("pytest_lock_holder")  # type: ignore[attr-defined]
    try:
        if held is None or holder != os.getppid():
            raise pytest_lock.LockNotHeldError("no lock from the parent")
        # Settings as configured: this fixture runs before the _gwN suffix is added.
        if Path(held) != pytest_lock.path_for(settings.DATABASES["default"]):
            raise pytest_lock.LockNotHeldError("the lock of another database")
        pytest_lock.check_held(Path(held), holder_pid=holder)
    except pytest_lock.LockNotHeldError as exc:
        pytest.exit(f"{WORKER_LOCK_MESSAGE} ({exc}).", returncode=3)


@pytest.fixture(scope="session", autouse=True)
def _test_clock() -> Generator[None]:
    """Run the whole suite as if it started at ``TEST_CLOCK`` (ISO 8601 with offset).

    The clock keeps ticking from there (tick=True) and the tests' own
    time_machine travels still nest inside. Used to find tests that depend on the
    wall-clock time of the run: a date boundary (23:30 UTC, 00:30 Paris) or a run
    after the fixed instants of the tests. Unset: the real clock.
        TEST_CLOCK=2026-11-13T23:30:00+00:00 uv run pytest
    """
    value = os.environ.get("TEST_CLOCK", "").strip()
    if not value:
        yield
        return
    instant = dt.datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise pytest.UsageError("TEST_CLOCK needs an explicit offset, e.g. +00:00")
    with time_machine.travel(instant, tick=True):
        yield


@pytest.fixture(scope="session")
def django_db_modify_db_settings(
    _pytest_lock: None,
    django_db_modify_db_settings_parallel_suffix: None,
    django_db_blocker: DjangoDbBlocker,
) -> Generator[None]:
    """Runs before pytest-django creates the test database: probe the server first."""
    # pytest-django blocks database access outside explicitly allowed scopes.
    with django_db_blocker.unblock():
        try:
            connections["default"].ensure_connection()
        except OperationalError:
            pytest.exit(DB_UNREACHABLE_MESSAGE, returncode=3)
        finally:
            connections["default"].close()
    yield


@pytest.fixture(autouse=True)
def _isolated_cache() -> Generator[None]:
    """Each test starts with an empty cache: allauth keeps rate-limit counters and
    TOTP anti-replay markers there, which would otherwise leak between tests."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _vault_storage_in_tmp(settings: Any, tmp_path: Path) -> None:
    """Blobs written by any test go to a per-test directory, never to var/ of the repo."""
    settings.VAULT_STORAGE_DIR = str(tmp_path / "vault")
    from apps.vault import storage

    storage.init_root()  # what `manage.py vault_init` does


@pytest.fixture(autouse=True)
def _vault_keys(request: pytest.FixtureRequest) -> None:
    """Version 1 of every KEK domain, as `manage.py vault_init` creates it, for every
    test that uses the database (rows only: no audit event, so tests counting
    events are unaffected). Opt out with @pytest.mark.without_vault_keys."""
    marker = request.node.get_closest_marker("django_db")
    if request.node.get_closest_marker("without_vault_keys"):
        return
    names = set(request.fixturenames)
    if marker is None and not names & {"db", "transactional_db"}:
        return
    transactional = "transactional_db" in names or bool(marker and marker.kwargs.get("transaction"))
    request.getfixturevalue("transactional_db" if transactional else "db")
    from apps.vault.formats import KeyDomain
    from apps.vault.models import KeyEncryptionKey

    for domain in KeyDomain:
        KeyEncryptionKey.objects.get_or_create(provider="env", domain=domain, version=1)


AUDIT_TABLES = ("vault_auditevent", "vault_auditcheckpoint")


@pytest.fixture(scope="session", autouse=True)
def _flush_bypasses_only_the_audit_truncate_triggers() -> Generator[None]:
    """transaction=True tests are cleaned up with TRUNCATE, which the append-only
    audit triggers forbid. Around Django's flush ONLY, the TRUNCATE triggers of
    the two audit tables are disabled, then enabled again (test database only)."""
    from django.db.backends.postgresql.operations import DatabaseOperations

    original = DatabaseOperations.sql_flush

    def sql_flush(self: Any, style: Any, tables: Any, **kwargs: Any) -> list[str]:
        statements = original(self, style, tables, **kwargs)
        guarded = [t for t in AUDIT_TABLES if t in tables]
        if not statements or not guarded:
            return list(statements)
        off = [f'ALTER TABLE "{t}" DISABLE TRIGGER "{t}_no_truncate";' for t in guarded]
        on = [f'ALTER TABLE "{t}" ENABLE TRIGGER "{t}_no_truncate";' for t in guarded]
        return [*off, *statements, *on]

    DatabaseOperations.sql_flush = sql_flush  # type: ignore[method-assign]
    yield
    DatabaseOperations.sql_flush = original  # type: ignore[method-assign]
