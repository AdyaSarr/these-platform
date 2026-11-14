"""Reopening (M3, revised 2026-11-14): the student's reasons are limited, the
cancelled cycle is kept as history, the validators whose approval is cancelled
by someone else are notified (a link only, rule 7)."""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail
from django.core.exceptions import ValidationError

from apps.accounts.roles import Role
from apps.admin_file import services
from apps.admin_file.models import (
    ItemNotification,
    ItemReopening,
    ItemStatus,
    Reason,
    RequiredItem,
    Section,
)
from apps.admin_file.tests.helpers import item_of, make_team
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db

S = ItemStatus
D, T = Role.DIRECTOR, Role.INDUSTRIAL_TUTOR
STUDENT_REASONS = {Reason.AMENDMENT, Reason.EXPIRED, Reason.OTHER}


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _validated(team: SimpleNamespace, roles: list[Role]) -> RequiredItem:
    section = Section.CIFRE if T in roles else Section.ACADEMIC
    item = item_of(team, section=section, validator_roles=roles)
    services.submit(item=item, user=team.student)
    for role in roles:
        services.approve(item=item, user=team.director if role == D else team.tutor)
    item.refresh_from_db()
    assert item.status == S.VALIDATED
    return item


def _reopen(item: RequiredItem, user: Any, capture: Any, reason: Reason = Reason.AMENDMENT) -> Any:
    mail.outbox.clear()
    with capture(execute=True):
        return services.reopen(item=item, user=user, reason=reason, comment="")


# --------------------------------------------------------------------------- reasons


@pytest.mark.parametrize("reason", list(Reason))
def test_the_student_reopens_with_three_reasons_only(team: SimpleNamespace, reason: Reason) -> None:
    item = _validated(team, [T])
    events = AuditEvent.objects.count()
    if reason in STUDENT_REASONS:
        assert services.reopen(item=item, user=team.student, reason=reason).status == S.TODO
        return
    with pytest.raises(ValidationError) as caught:
        services.reopen(item=item, user=team.student, reason=reason)
    assert caught.value.code == "reason"
    item.refresh_from_db()
    assert item.status == S.VALIDATED
    assert AuditEvent.objects.count() == events
    assert not ItemReopening.objects.exists()


@pytest.mark.parametrize("reason", list(Reason))
def test_a_validator_keeps_every_reason(team: SimpleNamespace, reason: Reason) -> None:
    item = _validated(team, [T])
    assert services.reopen(item=item, user=team.tutor, reason=reason).status == S.TODO


def test_the_reasons_offered(team: SimpleNamespace) -> None:
    item = _validated(team, [T])
    assert set(services.reopen_reasons_for(team.student, item)) == STUDENT_REASONS
    assert set(services.reopen_reasons_for(team.tutor, item)) == set(Reason)


# --------------------------------------------------------------------------- history


def test_the_cancelled_cycle_is_kept(team: SimpleNamespace) -> None:
    item = _validated(team, [T])
    validated_at = item.validated_at
    services.reopen(item=item, user=team.student, reason=Reason.EXPIRED, comment="Fictif")
    [row] = ItemReopening.objects.filter(item=item)
    assert (row.cycle, row.reopened_by, row.reason_code, row.reason_comment) == (
        1,
        team.student,
        Reason.EXPIRED,
        "Fictif",
    )
    assert row.validated_at == validated_at
    event = AuditEvent.objects.filter(action="ADMIN_ITEM_REOPENED").get()
    assert event.details_json["reason"] == "EXPIRED"
    assert "Fictif" not in str(event.details_json)


def test_a_second_reopening_of_a_later_cycle_is_kept_too(team: SimpleNamespace) -> None:
    item = _validated(team, [T])
    services.reopen(item=item, user=team.tutor, reason=Reason.AMENDMENT)
    services.submit(item=item, user=team.student)
    services.approve(item=item, user=team.tutor)
    services.reopen(item=item, user=team.tutor, reason=Reason.EXPIRED)
    assert list(ItemReopening.objects.filter(item=item).values_list("cycle", flat=True)) == [1, 2]


# --------------------------------------------------------------------------- notifications


def test_the_student_reopening_notifies_the_tutor(
    team: SimpleNamespace, django_capture_on_commit_callbacks: Any
) -> None:
    item = _validated(team, [T])
    _reopen(item, team.student, django_capture_on_commit_callbacks)
    [note] = ItemNotification.objects.all()
    assert (note.kind, note.recipient, note.status) == ("REOPENED", team.tutor, "SENT")
    assert [m.to for m in mail.outbox] == [[team.tutor.email]]
    event = AuditEvent.objects.filter(action="ADMIN_ITEM_REOPENED").get()
    assert event.details_json["notified"] == 1


def test_both_validators_are_notified_for_a_two_party_item(
    team: SimpleNamespace, django_capture_on_commit_callbacks: Any
) -> None:
    item = _validated(team, [D, T])
    _reopen(item, team.student, django_capture_on_commit_callbacks)
    assert sorted(m.to[0] for m in mail.outbox) == sorted([team.director.email, team.tutor.email])


def test_the_reopener_is_never_notified_of_their_own_approval(
    team: SimpleNamespace, django_capture_on_commit_callbacks: Any
) -> None:
    item = _validated(team, [D, T])
    _reopen(item, team.director, django_capture_on_commit_callbacks)
    assert [m.to for m in mail.outbox] == [[team.tutor.email]]
    single = _validated(team, [T])
    _reopen(single, team.tutor, django_capture_on_commit_callbacks)
    assert mail.outbox == []


def test_a_former_member_is_not_notified(
    team: SimpleNamespace, django_capture_on_commit_callbacks: Any
) -> None:
    from django.utils import timezone

    from apps.accounts.models import Membership

    item = _validated(team, [T])
    Membership.objects.filter(user=team.tutor).update(active_until=timezone.now())
    _reopen(item, team.student, django_capture_on_commit_callbacks)
    assert mail.outbox == []
    assert not ItemNotification.objects.exists()


def test_the_email_is_a_link_only(
    team: SimpleNamespace,
    django_capture_on_commit_callbacks: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    item = _validated(team, [T])
    caplog.set_level(logging.DEBUG)
    _reopen(item, team.student, django_capture_on_commit_callbacks)
    [message] = mail.outbox
    body = str(message.body)
    assert len(re.findall(r"https?://", body)) == 1
    assert f"/admin-file/{item.pk}/" in body
    for leak in (item.code, team.student.email, team.student.last_name, "AMENDMENT"):
        assert leak not in body and leak not in str(message.subject)
        assert leak not in caplog.text


def test_only_the_notification_id_goes_through_the_broker(
    team: SimpleNamespace, django_capture_on_commit_callbacks: Any
) -> None:
    from unittest import mock

    item = _validated(team, [T])
    with (
        mock.patch("apps.admin_file.tasks.send_item_notification.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        services.reopen(item=item, user=team.student, reason=Reason.AMENDMENT)
    note = ItemNotification.objects.get()
    delay.assert_called_once_with(str(note.pk))
