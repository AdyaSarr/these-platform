"""The student's export (M13b, docs/modules/M13-fin-de-these.md): rules here, views thin.

- export: THESIS_EXPORT (the student alone, a read: every status), a second factor
  less than 10 minutes old, at most one an hour (an interrupted one counts);
- the ST side: the tutor dates the end of the ST confidentiality
  (SET_ST_CONFIDENTIALITY) and grants a 30-day agreement (GRANT_ST_EXPORT), both
  with a second factor; revoking needs none (it closes an access).

An export is made in three steps (start_export): a READ ONLY snapshot (one
REPEATABLE READ transaction: one instant of the database, no vault lock, nothing
written), the opening (thesis row locked, hourly limit, record, alert e-mail,
THESIS_EXPORTED last), then the stream, outside any transaction. The vault writes
nothing: one vault service per transaction and the lock order hold by
construction.
"""

from __future__ import annotations

import datetime as dt
import functools
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import Membership, Thesis
from apps.accounts.permissions import Action, actions_for, can, roles_of
from apps.accounts.roles import Role
from apps.accounts.statuses import is_open_for
from apps.vault.errors import (
    BlobNotFoundError,
    ReauthenticationRequiredError,
    StorageError,
    UnknownKekError,
    VaultIntegrityError,
)

from . import archive, snapshot
from .models import ExportNotification, ExportRecord, StExportConsent

LIMIT = dt.timedelta(hours=1)
CONSENT_DAYS = 30


class ExportRateLimitedError(ValidationError):
    """One export an hour (the student only learns it: checked after the right)."""

    def __init__(self, next_at: dt.datetime) -> None:
        self.next_at = next_at
        super().__init__(_("One export an hour."), code="rate_limited")


class ExportTooLargeError(ValidationError):
    def __init__(self) -> None:
        super().__init__(
            _("The export would be too large. Try again with the latest versions only."),
            code="too_large",
        )


class ExportInterruptedError(Exception):
    """The stream stopped (a blob or a key gone meanwhile); journaled, recorded."""


# --------------------------------------------------------------------------- checks


def _require(user: Any, action: Action, thesis: Thesis) -> None:
    if not can(user, action, thesis):
        raise PermissionDenied


def _reauth(recent_mfa: bool) -> None:
    if not recent_mfa:
        raise ReauthenticationRequiredError


def _audit(action: str, user: Any, thesis: Thesis, **details: Any) -> None:
    from apps.vault.audit import AuditAction, audit

    audit(AuditAction(action), actor=user, thesis=thesis, **details)


def _last_export(user: Any, thesis: Thesis, now: dt.datetime) -> ExportRecord | None:
    return (
        ExportRecord.objects.filter(thesis=thesis, user=user, started_at__gt=now - LIMIT)
        .order_by("-started_at")
        .first()
    )


def _check_limit(user: Any, thesis: Thesis, now: dt.datetime) -> None:
    last = _last_export(user, thesis, now)
    if last is not None:
        raise ExportRateLimitedError(last.started_at + LIMIT)


def _students(thesis: Thesis) -> list[Any]:
    rows = Membership.objects.active().filter(thesis=thesis, role=Role.PHD_STUDENT)
    return [m.user for m in rows.select_related("user")]


def _queue(thesis: Thesis, kind: str, users: list[Any], **links: Any) -> None:
    """Rows in the caller's transaction; sent after commit, by their id only."""
    from .tasks import send_export_notification

    for user in users:
        row = ExportNotification.objects.create(thesis=thesis, recipient=user, kind=kind, **links)
        transaction.on_commit(functools.partial(send_export_notification.delay, str(row.pk)))


def _lower(counts: dict[str, int]) -> dict[str, int]:
    """Journal keys are lowercase identifiers (vault.audit): levels in lowercase."""
    return {k.lower(): v for k, v in sorted(counts.items()) if v}


# --------------------------------------------------------------------------- reads


def page(user: Any, thesis: Thesis) -> dict[str, Any]:
    """The export page of the student: history, the ST state, the hourly limit."""
    now = timezone.now()
    st_open, consent = snapshot.st_state(thesis, now)
    last = _last_export(user, thesis, now)
    return {
        "thesis": thesis,
        "history": list(
            ExportRecord.objects.filter(thesis=thesis, user=user).order_by("-started_at")[:20]
        ),
        "consent": consent,
        "st_open": st_open,
        "st_until": thesis.st_confidential_until,
        "next_at": last.started_at + LIMIT if last is not None else None,
    }


def st_page(user: Any, thesis: Thesis) -> dict[str, Any]:
    """The tutor's page: the end of the ST confidentiality and the agreements."""
    now = timezone.now()
    consents = StExportConsent.objects.filter(thesis=thesis).order_by("-created_at")[:20]
    return {
        "thesis": thesis,
        "st_until": thesis.st_confidential_until,
        "consents": [(c, c.is_valid(now)) for c in consents],
        "consent_days": CONSENT_DAYS,
    }


# --------------------------------------------------------------------------- the export


@dataclass
class StartedExport:
    record: ExportRecord
    snapshot: snapshot.Snapshot

    @property
    def filename(self) -> str:
        local = timezone.localtime(self.record.started_at)
        return f"export-these-{local:%Y%m%d-%H%M}.zip"

    def chunks(self) -> Generator[bytes]:
        """The ZIP, piece by piece; completion or interruption is recorded."""
        from apps.vault import services as vault

        try:
            yield from archive.stream(self.snapshot, record=self.record, read=vault.export_read)
        except GeneratorExit:
            _finish(self.record, "client_gone")
            raise
        except BlobNotFoundError:
            _finish(self.record, "blob_missing")
            raise ExportInterruptedError from None
        except UnknownKekError:
            _finish(self.record, "kek_unavailable")
            raise ExportInterruptedError from None
        except (VaultIntegrityError, StorageError):
            _finish(self.record, "integrity")
            raise ExportInterruptedError from None
        else:
            _finish(self.record, "")


def _finish(record: ExportRecord, reason: str) -> None:
    outcome = ExportRecord.Outcome.INCOMPLETE if reason else ExportRecord.Outcome.COMPLETE
    with transaction.atomic():
        ExportRecord.objects.filter(pk=record.pk).update(
            outcome=outcome, reason=reason, finished_at=timezone.now()
        )
        if reason:
            _audit("THESIS_EXPORT_INCOMPLETE", record.user, record.thesis,
                   export=str(record.pk), reason=reason)  # fmt: skip


def _take_snapshot(user: Any, thesis: Thesis, all_versions: bool, now: dt.datetime) -> Any:
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        fresh = Thesis.objects.get(pk=thesis.pk)
        return snapshot.take(user, fresh, all_versions=all_versions, now=now)


def start_export(
    *, thesis: Thesis, user: Any, all_versions: bool = False, recent_mfa: bool
) -> StartedExport:
    """Check, snapshot, open (journaled before the first byte); stream later."""
    if connection.in_atomic_block:
        raise RuntimeError("start_export makes its own transactions (CLAUDE.md, lock order)")
    _require(user, Action.THESIS_EXPORT, thesis)
    _reauth(recent_mfa)
    now = timezone.now()
    _check_limit(user, thesis, now)  # early, before the snapshot's work
    try:
        snap = _take_snapshot(user, thesis, all_versions, now)
    except snapshot.TextsTooLargeError:
        raise ExportTooLargeError from None
    if snap.total_bytes > settings.EXPORT_MAX_BYTES:
        raise ExportTooLargeError
    with transaction.atomic():
        locked = Thesis.objects.select_for_update().get(pk=thesis.pk)
        if not is_open_for(locked, "export"):
            raise PermissionDenied
        _require(user, Action.THESIS_EXPORT, locked)
        _check_limit(user, locked, timezone.now())  # under the lock: one of two at once
        record = ExportRecord.objects.create(
            thesis=locked, user=user, all_versions=all_versions, consent=snap.consent,
            created_by=user,
        )  # fmt: skip
        _queue(locked, ExportNotification.Kind.EXPORTED, [user], export=record)
        _audit(
            "THESIS_EXPORTED", user, locked,
            export=str(record.pk), all_versions=all_versions,
            st_consent=str(snap.consent.pk) if snap.consent else None,
            st_included=snap.st_included,
            documents=_lower(snap.counts.get("documents", {})),
            versions=_lower(snap.counts.get("versions", {})),
            texts=_lower(snap.counts.get("texts", {})),
            excluded=_lower(snap.excluded),
            confidential_ids=snap.confidential_ids,
        )  # fmt: skip
    return StartedExport(record=record, snapshot=snap)


# --------------------------------------------------------------------------- the ST side


def set_st_confidential_until(
    *, thesis: Thesis, user: Any, until: dt.date | None, recent_mfa: bool
) -> None:
    """The end of the ST confidentiality (empty: none known). The tutor only."""
    _require(user, Action.SET_ST_CONFIDENTIALITY, thesis)
    _reauth(recent_mfa)
    with transaction.atomic():
        locked = Thesis.objects.select_for_update().get(pk=thesis.pk)
        _require(user, Action.SET_ST_CONFIDENTIALITY, locked)
        old = locked.st_confidential_until
        if old == until:
            return
        locked.st_confidential_until = until
        locked.save(update_fields=["st_confidential_until", "updated_at"])
        _audit(
            "ST_CONFIDENTIALITY_SET",
            user,
            locked,
            old=old.isoformat() if old else None,
            new=until.isoformat() if until else None,
        )
    thesis.st_confidential_until = until


def grant_st_consent(*, thesis: Thesis, user: Any, recent_mfa: bool) -> StExportConsent:
    """INTERNAL_ST in the student's exports for 30 days. The tutor only."""
    _require(user, Action.GRANT_ST_EXPORT, thesis)
    _reauth(recent_mfa)
    with transaction.atomic():
        locked = Thesis.objects.select_for_update().get(pk=thesis.pk)
        _require(user, Action.GRANT_ST_EXPORT, locked)
        consent = StExportConsent.objects.create(
            thesis=locked, granted_by=user, valid_until=timezone.now(), created_by=user
        )
        consent.valid_until = consent.created_at + dt.timedelta(days=CONSENT_DAYS)
        consent.save(update_fields=["valid_until"])
        _queue(locked, ExportNotification.Kind.ST_CONSENT_GRANTED, _students(locked),
               consent=consent)  # fmt: skip
        _audit("ST_EXPORT_CONSENT_GRANTED", user, locked, consent=str(consent.pk),
               until=consent.valid_until.isoformat())  # fmt: skip
    return consent


def revoke_st_consent(*, consent: StExportConsent, user: Any) -> None:
    """Closes an access: allowed in every status, no second factor; a tutor of the
    thesis only (by role: the status never forbids a reduction)."""
    thesis = consent.thesis
    if Action.GRANT_ST_EXPORT not in actions_for(roles_of(user, thesis)):
        raise PermissionDenied
    with transaction.atomic():
        fresh = StExportConsent.objects.select_for_update().get(pk=consent.pk)
        if fresh.revoked_at is not None:
            return
        fresh.revoked_at, fresh.revoked_by = timezone.now(), user
        fresh.save(update_fields=["revoked_at", "revoked_by", "updated_at"])
        _audit("ST_EXPORT_CONSENT_REVOKED", user, thesis, consent=str(fresh.pk))
    consent.revoked_at = fresh.revoked_at
