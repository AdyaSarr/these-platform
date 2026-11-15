"""Send one queued notification e-mail at most once: the mechanism of every module.

Each module keeps its own rules, as two functions with the same contract:
``concerned(row)`` (the recipient is still an active member, with the role the
e-mail is for, and still reads the object) and ``still_due(row)`` (the object is
still in the state that calls for it). They are checked at SEND time, under the
row's lock, because the state may have changed since the row was queued.

This module imports no business module: the modules call it, never the reverse.
"""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Callable, Sequence
from typing import Any

from django.core.mail import BadHeaderError, send_mail
from django.db import models, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

THESIS_CLOSED = "thesis_closed"
NOT_CONCERNED = "not_concerned"
NOT_DUE = "not_due"
# The only words a skip may log: never a label, a title, an address or a reason.
SKIP_REASONS = frozenset({THESIS_CLOSED, NOT_CONCERNED, NOT_DUE})

_DONE = ("SENT", "SKIPPED")


def skip_reason(
    row: Any,
    *,
    thesis_open: bool,
    concerned: Callable[[Any], bool],
    still_due: Callable[[Any], bool],
) -> str | None:
    """Why ``row`` must not be sent, or None; each check runs only if needed."""
    if not thesis_open:
        return THESIS_CLOSED
    if not concerned(row):
        return NOT_CONCERNED
    if not still_due(row):
        return NOT_DUE
    return None


def deliver_once(
    model: type[models.Model],
    row_id: str,
    *,
    label: str,
    max_attempts: int,
    skip_reason: Callable[[Any], str | None],
    message: Callable[[Any], tuple[str, str]],
    select_related: Sequence[str] = (),
    lock_of: Sequence[str] = (),
) -> None:
    """E-mail the row ``row_id`` of ``model`` (status, attempts, sent_at, recipient).

    Under the row's lock (two workers send it once): a SENT or SKIPPED row, or
    one out of attempts, is left alone; then ``skip_reason(row)`` decides (SKIPPED,
    logged with the id and the code only); only then is ``message(row)`` built
    (subject, body: a link only, CLAUDE.md rule 7) and sent. No automatic retry:
    the daily runs queue a FAILED row again. ``lock_of`` limits the lock to the
    row itself when a joined relation is nullable (an outer join cannot be locked).
    """
    with transaction.atomic():
        query = model._default_manager.select_for_update(of=tuple(lock_of) or ())
        if select_related:
            query = query.select_related(*select_related)
        row: Any = query.filter(pk=row_id).first()
        if row is None or row.status in _DONE or row.attempts >= max_attempts:
            return
        reason = skip_reason(row)
        if reason is not None:
            if reason not in SKIP_REASONS:
                raise ValueError("a skip is logged with a fixed code only")
            row.status = "SKIPPED"
            row.save(update_fields=["status", "updated_at"])
            logger.info("%s skipped (%s, %s)", label, row.pk, reason)
            return
        subject, body = message(row)
        row.attempts += 1
        try:
            send_mail(subject, body, None, [row.recipient.email])
        except (smtplib.SMTPException, OSError, BadHeaderError):
            # The exception text may echo the message or the server: the id only.
            logger.warning("%s e-mail failed (%s)", label, row.pk, exc_info=False)
            row.status = "FAILED"
            row.save(update_fields=["status", "attempts", "updated_at"])
            return
        row.status = "SENT"
        row.sent_at = timezone.now()
        row.save(update_fields=["status", "attempts", "sent_at", "updated_at"])
