"""The locked delivery shared by the notification tasks (apps.core.delivery).

A row is sent at most once, under its lock; skip_reason() decides first, and the
message (subject, body, link) is built only for a row that is sent. A skipped row
is SKIPPED, logged with its id and a fixed code only, and nothing is sent.

The rows are admin_file reminders: any notification model of the four modules
has the same fields (status, attempts, sent_at, recipient).
"""

from __future__ import annotations

import logging
import re
import smtplib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final
from unittest import mock

import pytest
from django.core import mail

from apps.admin_file.models import ReminderLog
from apps.admin_file.tests.helpers import item_of, make_team
from apps.core import delivery

pytestmark = pytest.mark.django_db

LOGGER = "apps.core.delivery"


def _row() -> ReminderLog:
    team = make_team()
    item = item_of(team)
    assert item.due_date is not None  # the factory always gives one
    return ReminderLog.objects.create(
        item=item, kind="J14", due_date=item.due_date, recipient=team.student
    )


def _deliver(row: ReminderLog, *, reason: str | None = None, message: Any = None) -> None:
    delivery.deliver_once(
        ReminderLog,
        str(row.pk),
        label="test reminder",
        max_attempts=3,
        skip_reason=lambda _row: reason,
        message=message or (lambda _row: ("subject", "body https://example.test/x")),
        select_related=("recipient",),
    )


def test_a_due_row_is_sent_once() -> None:
    row = _row()
    _deliver(row)
    _deliver(row)
    row.refresh_from_db()
    assert (row.status, row.attempts) == ("SENT", 1)
    assert row.sent_at is not None
    assert [m.to for m in mail.outbox] == [[row.recipient.email]]


@pytest.mark.parametrize("code", sorted(delivery.SKIP_REASONS))
def test_a_skipped_row_builds_no_message_and_sends_nothing(
    code: str, caplog: pytest.LogCaptureFixture
) -> None:
    row = _row()
    message = mock.Mock(return_value=("subject", "body"))
    caplog.set_level(logging.INFO, logger=LOGGER)
    _deliver(row, reason=code, message=message)
    message.assert_not_called()
    row.refresh_from_db()
    assert (row.status, row.attempts, row.sent_at) == ("SKIPPED", 0, None)
    assert mail.outbox == []
    assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == [
        f"test reminder skipped ({row.pk}, {code})"
    ]


def test_a_skipped_row_queued_again_is_left_alone(caplog: pytest.LogCaptureFixture) -> None:
    row = _row()
    _deliver(row, reason="not_due")
    caplog.set_level(logging.INFO, logger=LOGGER)
    caplog.clear()
    skip_reason = mock.Mock(return_value=None)
    message = mock.Mock(return_value=("subject", "body"))
    delivery.deliver_once(
        ReminderLog,
        str(row.pk),
        label="test reminder",
        max_attempts=3,
        skip_reason=skip_reason,
        message=message,
    )
    skip_reason.assert_not_called()
    message.assert_not_called()
    row.refresh_from_db()
    assert row.status == "SKIPPED"
    assert mail.outbox == []
    assert caplog.records == []


def test_a_sent_or_exhausted_row_is_not_checked_again() -> None:
    row = _row()
    _deliver(row)
    exhausted = _row()
    ReminderLog.objects.filter(pk=exhausted.pk).update(status="FAILED", attempts=3)
    for target in (row, exhausted):
        skip_reason = mock.Mock(return_value=None)
        delivery.deliver_once(
            ReminderLog,
            str(target.pk),
            label="test reminder",
            max_attempts=3,
            skip_reason=skip_reason,
            message=lambda _row: ("subject", "body"),
        )
        skip_reason.assert_not_called()
    assert len(mail.outbox) == 1


def test_an_unknown_row_does_nothing() -> None:
    skip_reason = mock.Mock(return_value=None)
    delivery.deliver_once(
        ReminderLog,
        "6b0f6a4e-0000-4000-8000-000000000000",
        label="test reminder",
        max_attempts=3,
        skip_reason=skip_reason,
        message=lambda _row: ("subject", "body"),
    )
    skip_reason.assert_not_called()
    assert mail.outbox == []


def test_a_free_text_reason_is_refused_before_anything_is_written(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only fixed codes reach the log: never a label, an address or a title."""
    row = _row()
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    with pytest.raises(ValueError):
        _deliver(row, reason=f"no longer {row.recipient.email}")
    row.refresh_from_db()
    assert row.status == "PENDING"
    assert row.recipient.email not in caplog.text


def test_a_failed_e_mail_logs_the_id_only(caplog: pytest.LogCaptureFixture) -> None:
    row = _row()
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    with mock.patch(
        "apps.core.delivery.send_mail", side_effect=smtplib.SMTPException(row.recipient.email)
    ):
        _deliver(row)
    row.refresh_from_db()
    assert (row.status, row.attempts) == ("FAILED", 1)
    assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == [
        f"test reminder e-mail failed ({row.pk})"
    ]
    assert row.recipient.email not in caplog.text


def test_skip_reason_checks_in_order_and_lazily() -> None:
    """thesis_closed, then not_concerned, then not_due; a later check is not run."""
    never = mock.Mock(side_effect=AssertionError("not called"))
    assert delivery.skip_reason(None, thesis_open=False, concerned=never, still_due=never) == (
        "thesis_closed"
    )
    no = mock.Mock(return_value=False)
    assert delivery.skip_reason(None, thesis_open=True, concerned=no, still_due=never) == (
        "not_concerned"
    )
    yes = mock.Mock(return_value=True)
    assert delivery.skip_reason(None, thesis_open=True, concerned=yes, still_due=no) == "not_due"
    assert delivery.skip_reason(None, thesis_open=True, concerned=yes, still_due=yes) is None


def test_the_thesis_says_whether_it_is_open() -> None:
    from apps.accounts.models import Thesis

    assert [s for s in Thesis.Status if Thesis(status=s).is_open] == [
        Thesis.Status.SETUP,
        Thesis.Status.ACTIVE,
    ]


NOTIFYING_MODULES = (
    "admin_file", "meetings", "conferences", "travel", "activity", "lifecycle", "export",
    "portfolio",
)  # fmt: skip


@pytest.mark.parametrize("module", NOTIFYING_MODULES)
def test_every_notification_task_goes_through_the_shared_delivery(module: str) -> None:
    """Static guard: no module sends its notifications by itself, so none can skip
    the send-time check (concerned, still_due, finished thesis)."""
    import importlib
    import inspect

    source = inspect.getsource(importlib.import_module(f"apps.{module}.tasks"))
    assert "deliver_once(" in source
    assert "send_mail" not in source
    notifications = importlib.import_module(f"apps.{module}.notifications")
    for name in ("concerned", "still_due", "skip_reason"):
        assert callable(getattr(notifications, name, None)), (module, name)


# --------------------------------------------------------------------------- every sender (SR-04)

APPS = Path(__file__).resolve().parents[2]
ROOT = APPS.parent
# Any use of Django's or Python's mail API, call or not.
MAIL_API = re.compile(
    r"\b(send_mail|send_mass_mail|mail_admins|mail_managers|EmailMessage|"
    r"EmailMultiAlternatives|get_connection|smtplib)\b"
)
# A call that sends (or builds a message to send).
SEND_CALL = re.compile(
    r"\b(send_mail|send_mass_mail|mail_admins|mail_managers|EmailMessage|"
    r"EmailMultiAlternatives)\s*\("
)

# Closed list of the files that send e-mail without apps.core.delivery.deliver_once.
# A new entry is a reviewed decision (docs/security/revue-2026-11.md, SR-04).
DIRECT_SENDERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        # The shared delivery itself: the send-time checks of every notification module.
        "apps/core/delivery.py": "deliver_once",
        # Invitation and password-reset links: a request of the person, sent at once,
        # nothing to re-check later (no thesis state, no recurring row).
        "apps/accounts/tasks.py": "invitation and password-reset links",
        # Weekly audit checkpoint to the PHD_STUDENT only: the one exception of rule 7
        # (seq, hash, signature, timestamp), docs/decisions.md.
        "apps/vault/tasks.py": "audit checkpoint",
    }
)
# Files that use the mail API without sending anything: closed list as well.
MAIL_API_READERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        # The demonstration reads the in-memory (locmem) outbox to take the links that
        # accounts.tasks sent; mail.EmailMessage appears in type annotations only.
        "apps/demo/scenario.py": "reads the locmem outbox",
    }
)


def _mail_api_users() -> set[str]:
    return {
        path.relative_to(ROOT).as_posix()
        for path in APPS.rglob("*.py")
        if "tests" not in path.parts and "migrations" not in path.parts
        and MAIL_API.search(path.read_text(encoding="utf-8"))
    }  # fmt: skip


def test_every_use_of_the_mail_api_is_declared() -> None:
    """Static guard over the whole of apps/, not a list of modules: a new sender
    anywhere fails here until it is reviewed and declared."""
    found = _mail_api_users()
    declared = set(DIRECT_SENDERS) | set(MAIL_API_READERS)
    assert found - declared == set(), "undeclared"
    assert declared - found == set(), "stale entry: no use of the mail API any more"


@pytest.mark.parametrize("path", sorted(DIRECT_SENDERS))
def test_every_declared_sender_still_sends(path: str) -> None:
    """No stale exception: an entry that no longer sends must leave the list."""
    assert SEND_CALL.search((ROOT / path).read_text(encoding="utf-8")), path


@pytest.mark.parametrize("path", sorted(MAIL_API_READERS))
def test_a_declared_reader_sends_nothing_and_still_reads(path: str) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    assert not SEND_CALL.search(source), path
    assert "mail.outbox" in source, path


def test_every_module_that_delivers_is_in_the_notifying_list() -> None:
    delivering = {
        path.parent.name
        for path in APPS.glob("*/tasks.py")
        if "deliver_once(" in path.read_text(encoding="utf-8")
    }
    assert delivering == set(NOTIFYING_MODULES)


def test_the_send_call_pattern_sees_what_it_must() -> None:
    assert SEND_CALL.search("        send_mail(subject, body, None, [to])")
    assert SEND_CALL.search("EmailMultiAlternatives (subject)")
    assert not SEND_CALL.search("outbox: list[mail.EmailMessage]")
