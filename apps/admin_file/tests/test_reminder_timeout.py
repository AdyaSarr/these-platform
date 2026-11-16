"""A stuck SMTP server: the reminder's sending gives up after EMAIL_TIMEOUT, the
row lock is released, and the attempt is counted (FAILED, retried later).

A real socket that accepts the connection and never answers (no greeting), and
Django's real SMTP backend: the timeout is the one the backend applies.
"""

from __future__ import annotations

import datetime as dt
import socket
import threading
import time
from collections.abc import Iterator

import pytest
import time_machine
from django.db import connection, transaction

from apps.admin_file import reminders
from apps.admin_file.models import ReminderLog
from apps.admin_file.tests.helpers import item_of, make_team

pytestmark = pytest.mark.django_db(transaction=True)

SILENCE = 30.0  # the server closes after this: a missing timeout cannot hang the suite


@pytest.fixture
def silent_smtp() -> Iterator[int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    accepted: list[socket.socket] = []

    def serve() -> None:
        server.settimeout(SILENCE)
        try:
            conn, _ = server.accept()
        except OSError:
            return
        accepted.append(conn)
        time.sleep(SILENCE)  # never sends the 220 greeting
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield server.getsockname()[1]
    server.close()
    for conn in accepted:
        conn.close()


@pytest.mark.serial
def test_a_stuck_smtp_server_times_out_and_releases_the_row(
    settings: pytest.FixtureRequest, silent_smtp: int
) -> None:
    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"  # type: ignore[attr-defined]
    settings.EMAIL_HOST = "127.0.0.1"  # type: ignore[attr-defined]
    settings.EMAIL_PORT = silent_smtp  # type: ignore[attr-defined]
    settings.EMAIL_USE_TLS = False  # type: ignore[attr-defined]
    settings.EMAIL_TIMEOUT = 1  # type: ignore[attr-defined]  # 10 in base and prod (test_settings)
    team = make_team()
    item_of(team, due_date=dt.date(2026, 12, 10))

    started = time.monotonic()
    with time_machine.travel(dt.datetime(2026, 12, 1, 9, 0, tzinfo=dt.UTC), tick=True):
        reminders.run()  # the eager task runs here, against the silent server
    elapsed = time.monotonic() - started

    assert elapsed < 10, elapsed  # gave up after EMAIL_TIMEOUT, not after SILENCE
    reminder = ReminderLog.objects.get()
    assert (reminder.status, reminder.attempts) == (ReminderLog.Status.FAILED, 1)
    assert reminder.sent_at is None
    assert not connection.in_atomic_block
    # Another connection can lock the row at once: the sending transaction is over.
    locked: list[str] = []

    def lock_now() -> None:
        try:
            with transaction.atomic():
                row = ReminderLog.objects.select_for_update(nowait=True).get(pk=reminder.pk)
                locked.append(row.status)
        finally:
            connection.close()

    other = threading.Thread(target=lock_now)
    other.start()
    other.join(10)
    assert locked == [ReminderLog.Status.FAILED]
