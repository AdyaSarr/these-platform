"""The passages of M13a (docs/modules/M13-fin-de-these.md, « Passages »), at service level."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from apps.accounts.models import Thesis
from apps.accounts.roles import Role
from apps.lifecycle import services
from apps.lifecycle.models import ClosureRequest, LifecycleNotification
from apps.lifecycle.tests import helpers as h
from apps.vault.errors import ReauthenticationRequiredError

pytestmark = pytest.mark.django_db(transaction=True)
S = Thesis.Status
OTHERS = ("student", "co_supervisor", "tutor", "st_co_supervisor", "csi", "guest", "outsider")


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _code(exc: Any) -> str:
    return str(exc.value.code)


# --------------------------------------------------------------------------- ACTIVE -> DEFENDED


def test_the_director_declares_the_defence(team: SimpleNamespace) -> None:
    thesis = h.defended(team)
    assert (thesis.status, thesis.defended_on) == (S.DEFENDED, h.DEFENSE)
    assert h.status_of(team) == S.DEFENDED
    (details,) = h.audited("THESIS_DEFENDED")
    assert details == {"defended_on": "2027-06-01"}


@pytest.mark.parametrize("role", OTHERS)
def test_no_one_else_declares_it(team: SimpleNamespace, role: str) -> None:
    with pytest.raises(PermissionDenied):
        services.declare_defense(
            thesis=team.thesis, user=getattr(team, role), defended_on=h.DEFENSE, recent_mfa=True
        )
    assert h.status_of(team) == S.ACTIVE


def test_the_declaration_needs_a_recent_second_factor(team: SimpleNamespace) -> None:
    with pytest.raises(ReauthenticationRequiredError):
        services.declare_defense(
            thesis=team.thesis, user=team.director, defended_on=h.DEFENSE, recent_mfa=False
        )
    assert h.status_of(team) == S.ACTIVE


@pytest.mark.parametrize("day", [h.D(2027, 6, 11), h.D(2026, 12, 14)])
def test_the_defence_date_is_past_and_after_the_first_enrollment(
    team: SimpleNamespace, day: dt.date
) -> None:
    with pytest.raises(ValidationError) as exc:
        h.defended(team, day)
    assert _code(exc) == "date"
    assert h.status_of(team) == S.ACTIVE


@pytest.mark.parametrize("status", [S.SETUP, S.DEFENDED, S.CLOSED])
def test_only_an_active_thesis_is_declared_defended(team: SimpleNamespace, status: str) -> None:
    Thesis.objects.filter(pk=team.thesis.pk).update(status=status)
    with pytest.raises((PermissionDenied, ValidationError)):
        h.defended(team)
    assert h.status_of(team) == status


def test_the_open_invitations_are_revoked_at_the_defence(team: SimpleNamespace) -> None:
    from apps.accounts import services as accounts
    from apps.accounts.models import Invitation, Membership

    org = Membership.objects.filter(thesis=team.thesis).first().organization  # type: ignore[union-attr]
    inv = accounts.create_invitation(
        thesis=team.thesis,
        email="invite.fictif@example.org",
        role=Role.CO_SUPERVISOR,
        organization=org,
        by=team.student,
    )
    h.defended(team)
    assert Invitation.objects.get(pk=inv.pk).revoked_at is not None
    assert {"reason": "thesis_defended"}.items() <= h.audited("INVITATION_REVOKED")[-1].items()


# --------------------------------------------------------------------------- DEFENDED -> ACTIVE


@pytest.mark.parametrize("reason", ["wrong_date", "declared_by_mistake", "postponed"])
def test_the_director_cancels_the_declaration_with_a_reason(
    team: SimpleNamespace, reason: str
) -> None:
    h.defended(team)
    thesis = services.cancel_defense(
        thesis=team.thesis, user=team.director, reason=reason, recent_mfa=True
    )
    assert (thesis.status, thesis.defended_on) == (S.ACTIVE, None)
    assert h.audited("THESIS_DEFENSE_CANCELLED") == [{"reason": reason}]


def test_a_cancellation_needs_a_reason_of_the_list(team: SimpleNamespace) -> None:
    h.defended(team)
    with pytest.raises(ValidationError) as exc:
        services.cancel_defense(
            thesis=team.thesis, user=team.director, reason="TEMOIN-FUITE-MOTIF", recent_mfa=True
        )
    assert _code(exc) == "reason"
    assert h.status_of(team) == S.DEFENDED


@pytest.mark.parametrize("role", OTHERS)
def test_no_one_else_cancels_it(team: SimpleNamespace, role: str) -> None:
    h.defended(team)
    with pytest.raises(PermissionDenied):
        services.cancel_defense(
            thesis=team.thesis, user=getattr(team, role), reason="wrong_date", recent_mfa=True
        )
    assert h.status_of(team) == S.DEFENDED


def test_the_cancellation_needs_a_recent_second_factor(team: SimpleNamespace) -> None:
    h.defended(team)
    with pytest.raises(ReauthenticationRequiredError):
        services.cancel_defense(
            thesis=team.thesis, user=team.director, reason="wrong_date", recent_mfa=False
        )


def test_the_cancellation_withdraws_an_open_closure_request(
    team: SimpleNamespace, time_machine: Any
) -> None:
    h.defended(team)
    time_machine.move_to(h.CLOSABLE, tick=True)
    request = h.requested(team)
    services.cancel_defense(
        thesis=team.thesis, user=team.director, reason="wrong_date", recent_mfa=True
    )
    assert ClosureRequest.objects.get(pk=request.pk).withdrawn_at is not None


# --------------------------------------------------------------------------- closure: request


@pytest.mark.parametrize("by", ["director", "student"])
def test_the_director_or_the_student_requests_the_closure(
    team: SimpleNamespace, time_machine: Any, by: str
) -> None:
    h.defended(team)
    time_machine.move_to(h.CLOSABLE, tick=True)
    request = h.requested(team, getattr(team, by))
    assert request.is_open and request.reason == "defended"
    assert abs(request.expires_at - (h.CLOSABLE + dt.timedelta(days=14))) < dt.timedelta(minutes=1)
    assert h.audited("THESIS_CLOSURE_REQUESTED") == [{"reason": "defended"}]
    assert h.status_of(team) == S.DEFENDED


@pytest.mark.parametrize("role", ["co_supervisor", "tutor", "st_co_supervisor", "csi", "guest",
                                  "outsider"])  # fmt: skip
def test_no_one_else_requests_it(team: SimpleNamespace, time_machine: Any, role: str) -> None:
    h.defended(team)
    time_machine.move_to(h.CLOSABLE, tick=True)
    with pytest.raises(PermissionDenied):
        h.requested(team, getattr(team, role))
    assert not ClosureRequest.objects.exists()


def test_the_request_needs_a_recent_second_factor(team: SimpleNamespace, time_machine: Any) -> None:
    h.defended(team)
    time_machine.move_to(h.CLOSABLE, tick=True)
    with pytest.raises(ReauthenticationRequiredError):
        services.request_closure(
            thesis=team.thesis, user=team.director, reason="defended", recent_mfa=False
        )


def test_not_before_the_minimal_delay_after_the_defence(
    team: SimpleNamespace, time_machine: Any, settings: Any
) -> None:
    h.defended(team)
    time_machine.move_to(
        dt.datetime(2027, 8, 31, 22, 30, tzinfo=dt.UTC), tick=True
    )  # Paris: 1 Sept.
    h.requested(team)  # 3 months by default: 2027-09-01, in Paris
    LifecycleNotification.objects.all().delete()
    ClosureRequest.objects.all().delete()
    time_machine.move_to(dt.datetime(2027, 8, 31, 9, 0, tzinfo=dt.UTC), tick=True)
    with pytest.raises(ValidationError) as exc:
        h.requested(team)
    assert _code(exc) == "too_early"
    settings.THESIS_CLOSE_MIN_MONTHS = 2  # a setting, « à confirmer (Q4) »
    h.requested(team)


@pytest.mark.parametrize("reason", ["student_withdrew", "cifre_terminated", "other"])
def test_an_active_thesis_is_closed_on_abandonment_only(team: SimpleNamespace, reason: str) -> None:
    with pytest.raises(ValidationError) as exc:
        h.requested(team, reason="defended")
    assert _code(exc) == "reason"
    request = h.requested(team, reason=reason)
    assert request.reason == reason


def test_a_defended_thesis_is_closed_as_defended_only(
    team: SimpleNamespace, time_machine: Any
) -> None:
    h.defended(team)
    time_machine.move_to(h.CLOSABLE, tick=True)
    with pytest.raises(ValidationError) as exc:
        h.requested(team, reason="student_withdrew")
    assert _code(exc) == "reason"


def test_a_reason_out_of_the_list_is_refused(team: SimpleNamespace) -> None:
    with pytest.raises(ValidationError) as exc:
        h.requested(team, reason="TEMOIN-FUITE-MOTIF")
    assert _code(exc) == "reason"


def test_one_open_request_per_thesis(team: SimpleNamespace) -> None:
    h.requested(team, reason="other")
    with pytest.raises(ValidationError) as exc:
        h.requested(team, team.student, reason="other")
    assert _code(exc) == "open_request"
    first = ClosureRequest.objects.get()
    with pytest.raises(IntegrityError), transaction.atomic():  # the database says so too
        ClosureRequest.objects.create(
            thesis=team.thesis, requested_by=team.student, requester_role="PHD_STUDENT",
            reason="other", expires_at=first.expires_at,
        )  # fmt: skip


def test_a_setup_thesis_is_never_closed(team: SimpleNamespace) -> None:
    Thesis.objects.filter(pk=team.thesis.pk).update(status=S.SETUP)
    with pytest.raises((PermissionDenied, ValidationError)):
        h.requested(team, reason="other")


# --------------------------------------------------------------------------- closure: confirm


@pytest.mark.parametrize(("by", "confirmer"), [("director", "student"), ("student", "director")])
def test_the_other_party_confirms(
    team: SimpleNamespace, time_machine: Any, by: str, confirmer: str
) -> None:
    h.defended(team)
    time_machine.move_to(h.CLOSABLE, tick=True)
    request = h.requested(team, getattr(team, by))
    thesis = services.confirm_closure(
        request=request, user=getattr(team, confirmer), recent_mfa=True
    )
    assert thesis.status == S.CLOSED and h.status_of(team) == S.CLOSED
    assert thesis.closed_at is not None
    assert thesis.access_until == h.D(2027, 12, 2)  # closure + 3 months (setting)
    assert ClosureRequest.objects.get(pk=request.pk).confirmed_by_id == getattr(team, confirmer).pk
    assert h.audited("THESIS_CLOSED") == [{"reason": "defended"}]


@pytest.mark.parametrize("by", ["director", "student"])
def test_the_requester_never_confirms_their_own_request(
    team: SimpleNamespace, time_machine: Any, by: str
) -> None:
    h.defended(team)
    time_machine.move_to(h.CLOSABLE, tick=True)
    request = h.requested(team, getattr(team, by))
    with pytest.raises((PermissionDenied, ValidationError)):
        services.confirm_closure(request=request, user=getattr(team, by), recent_mfa=True)
    assert h.status_of(team) == S.DEFENDED


def test_a_second_director_does_not_confirm_a_director_request(
    team: SimpleNamespace, time_machine: Any
) -> None:
    from apps.accounts.tests.factories import MembershipFactory

    other = MembershipFactory.create(thesis=team.thesis, role="DIRECTOR").user
    h.defended(team)
    time_machine.move_to(h.CLOSABLE, tick=True)
    request = h.requested(team)
    with pytest.raises((PermissionDenied, ValidationError)):
        services.confirm_closure(request=request, user=other, recent_mfa=True)
    services.confirm_closure(request=request, user=team.student, recent_mfa=True)


@pytest.mark.parametrize("role", ["co_supervisor", "tutor", "st_co_supervisor", "csi", "guest",
                                  "outsider"])  # fmt: skip
def test_no_one_else_confirms(team: SimpleNamespace, time_machine: Any, role: str) -> None:
    h.defended(team)
    time_machine.move_to(h.CLOSABLE, tick=True)
    request = h.requested(team)
    with pytest.raises(PermissionDenied):
        services.confirm_closure(request=request, user=getattr(team, role), recent_mfa=True)
    assert h.status_of(team) == S.DEFENDED


def test_the_confirmation_needs_a_recent_second_factor(
    team: SimpleNamespace, time_machine: Any
) -> None:
    h.defended(team)
    time_machine.move_to(h.CLOSABLE, tick=True)
    request = h.requested(team)
    with pytest.raises(ReauthenticationRequiredError):
        services.confirm_closure(request=request, user=team.student, recent_mfa=False)
    assert h.status_of(team) == S.DEFENDED


def test_an_expired_request_is_not_confirmed(team: SimpleNamespace, time_machine: Any) -> None:
    h.defended(team)
    time_machine.move_to(h.CLOSABLE, tick=True)
    request = h.requested(team)
    time_machine.move_to(h.CLOSABLE + dt.timedelta(days=14, seconds=1), tick=True)
    with pytest.raises(ValidationError) as exc:
        services.confirm_closure(request=request, user=team.student, recent_mfa=True)
    assert _code(exc) == "not_open"
    assert h.status_of(team) == S.DEFENDED


def test_a_withdrawn_request_is_not_confirmed(team: SimpleNamespace, time_machine: Any) -> None:
    h.defended(team)
    time_machine.move_to(h.CLOSABLE, tick=True)
    request = h.requested(team)
    services.withdraw_closure(request=request, user=team.student)  # the other party may
    with pytest.raises(ValidationError) as exc:
        services.confirm_closure(request=request, user=team.student, recent_mfa=True)
    assert _code(exc) == "not_open"
    assert h.audited("THESIS_CLOSURE_WITHDRAWN") == [{"by": "counterpart"}]


def test_the_status_is_checked_again_at_confirmation(
    team: SimpleNamespace, time_machine: Any
) -> None:
    """A request for an abandonment made while ACTIVE, then a defence declared:
    the request (withdrawn by the declaration) cannot close the thesis."""
    request = h.requested(team, reason="other")
    h.defended(team)
    with pytest.raises(ValidationError):
        services.confirm_closure(request=request, user=team.student, recent_mfa=True)
    assert h.status_of(team) == S.DEFENDED


def test_an_abandonment_closes_an_active_thesis(team: SimpleNamespace) -> None:
    request = h.requested(team, team.student, reason="student_withdrew")
    thesis = services.confirm_closure(request=request, user=team.director, recent_mfa=True)
    assert thesis.status == S.CLOSED and thesis.defended_on is None
    assert h.audited("THESIS_CLOSED") == [{"reason": "student_withdrew"}]


# --------------------------------------------------------------------------- withdrawal, expiry


@pytest.mark.parametrize(("by", "who"), [("director", "requester"), ("student", "counterpart")])
def test_either_party_withdraws_without_a_second_factor(
    team: SimpleNamespace, by: str, who: str
) -> None:
    request = h.requested(team, reason="other")
    services.withdraw_closure(request=request, user=getattr(team, by))
    assert not ClosureRequest.objects.get(pk=request.pk).is_open
    assert h.audited("THESIS_CLOSURE_WITHDRAWN") == [{"by": who}]


@pytest.mark.parametrize("role", ["co_supervisor", "tutor", "csi", "guest", "outsider"])
def test_no_one_else_withdraws(team: SimpleNamespace, role: str) -> None:
    request = h.requested(team, reason="other")
    with pytest.raises(PermissionDenied):
        services.withdraw_closure(request=request, user=getattr(team, role))
    assert ClosureRequest.objects.get(pk=request.pk).is_open


def test_the_daily_run_expires_a_request_after_14_days(
    team: SimpleNamespace, time_machine: Any
) -> None:
    request = h.requested(team, reason="other")
    time_machine.move_to(h.NOW + dt.timedelta(days=13), tick=True)
    services.daily()
    assert ClosureRequest.objects.get(pk=request.pk).is_open
    time_machine.move_to(h.NOW + dt.timedelta(days=14, hours=1), tick=True)
    assert services.daily()["expired"] == 1
    assert services.daily()["expired"] == 0  # once
    assert ClosureRequest.objects.get(pk=request.pk).expired_at is not None
    assert len(h.audited("THESIS_CLOSURE_EXPIRED")) == 1


# --------------------------------------------------------------------------- CLOSED


def test_closed_is_irreversible(team: SimpleNamespace, time_machine: Any) -> None:
    h.closed(team, time_machine)
    for attempt in (
        lambda: h.defended(team),
        lambda: services.cancel_defense(
            thesis=team.thesis, user=team.director, reason="wrong_date", recent_mfa=True
        ),
        lambda: h.requested(team),
        lambda: h.requested(team, team.student, reason="other"),
    ):
        with pytest.raises((PermissionDenied, ValidationError)):
            attempt()
    assert h.status_of(team) == S.CLOSED


def test_no_service_leaves_closed() -> None:
    """Static: in the lifecycle services, every write of a status names a status
    other than CLOSED only after checking it is not CLOSED; the only assignments
    of a status are DEFENDED, ACTIVE (cancel) and CLOSED (confirm)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(services))
    assigned = {
        node.value.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "status" for t in node.targets)
        and isinstance(node.value, ast.Attribute)
    }
    assert assigned == {"DEFENDED", "ACTIVE", "CLOSED"}


def test_the_journal_carries_codes_only(team: SimpleNamespace, time_machine: Any) -> None:
    h.closed(team, time_machine)
    for action in ("THESIS_DEFENDED", "THESIS_CLOSURE_REQUESTED", "THESIS_CLOSED"):
        for details in h.audited(action):
            assert set(details) <= {"defended_on", "reason"}
