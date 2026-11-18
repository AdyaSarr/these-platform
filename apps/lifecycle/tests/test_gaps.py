"""The holes of M2 and M1 (M13a, criterion 1): refused on a CLOSED thesis, and the
refusal cannot be told from a refusal for lack of right.

Each case is played twice: the student on a CLOSED thesis, and a role without the
right on an ACTIVE thesis. Same exception, same journal lines (action, object
type, details), same HTTP answer.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import Membership, Thesis
from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.lifecycle.tests import helpers as h
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)


def _pdf() -> SimpleUploadedFile:
    return SimpleUploadedFile("piece.pdf", b"%PDF-1.7\n%piece fictive\n%%EOF\n")


def _org(team: SimpleNamespace) -> Any:
    return Membership.objects.filter(thesis=team.thesis).first().organization  # type: ignore[union-attr]


def _member(team: SimpleNamespace, attr: str) -> Membership:
    return Membership.objects.active().get(thesis=team.thesis, user=getattr(team, attr))


def _close(team: SimpleNamespace) -> None:
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.CLOSED)


def _outcome(call: Callable[[], Any]) -> tuple[str, list[tuple[str, str, dict[str, Any]]]]:
    """The exception class and the journal lines the call wrote (ids aside)."""
    before = AuditEvent.objects.order_by("-seq").values_list("seq", flat=True).first() or 0
    try:
        call()
    except Exception as exc:  # compared below
        raised = type(exc).__name__
    else:
        raised = "nothing"
    rows = AuditEvent.objects.filter(seq__gt=before).order_by("seq")
    return raised, [(r.action, r.object_type, r.details_json) for r in rows]


# Each case: (what to prepare on a team, the call by ``user``).
Case = Callable[[SimpleNamespace, Any], Callable[[], Any]]


def _store(team: SimpleNamespace, user: Any) -> Callable[[], Any]:
    from apps.vault import services as vault

    return lambda: vault.store(
        thesis=team.thesis, upload=_pdf(), title="Note fictive", level=L.PUBLIC, user=user
    )


def _add_version(team: SimpleNamespace, user: Any) -> Callable[[], Any]:
    from apps.vault import services as vault

    document = h_document(team)
    return lambda: vault.add_version(document=document, upload=_pdf(), user=user)


def _change_level(team: SimpleNamespace, user: Any) -> Callable[[], Any]:
    from apps.vault import services as vault

    document = h_document(team)
    return lambda: vault.change_level(document=document, level=L.INTERNAL_LIRMM, user=user)


def _grant(team: SimpleNamespace, user: Any) -> Callable[[], Any]:
    from apps.vault import services as vault

    document = h_document(team, L.CONFIDENTIAL)
    return lambda: vault.grant_access(document=document, grantee=team.tutor, user=user)


def _revoke(team: SimpleNamespace, user: Any) -> Callable[[], Any]:
    from apps.vault import services as vault

    document = h_document(team, L.CONFIDENTIAL)
    grant = vault.grant_access(document=document, grantee=team.tutor, user=team.student)
    return lambda: vault.revoke_access(grant=grant, user=user)


def _sealing(team: SimpleNamespace, user: Any) -> Callable[[], Any]:
    from apps.accounts.permissions import Action
    from apps.vault import services as vault

    def call() -> None:
        with vault.sealing(user=user, thesis=team.thesis, level=L.PUBLIC,
                           action=Action.DOCUMENT_WRITE):  # fmt: skip
            pass

    return call


def _invite(team: SimpleNamespace, user: Any) -> Callable[[], Any]:
    from apps.accounts import services as accounts

    return lambda: accounts.create_invitation(
        thesis=team.thesis, email="invite.fictif@example.org", role=Role.CO_SUPERVISOR,
        organization=_org(team), by=user,
    )  # fmt: skip


def _revoke_invitation(team: SimpleNamespace, user: Any) -> Callable[[], Any]:
    from apps.accounts import services as accounts

    inv = _invite(team, team.student)()
    return lambda: accounts.revoke_invitation(inv, by=user)


def _change_role(team: SimpleNamespace, user: Any) -> Callable[[], Any]:
    from apps.accounts import services as accounts

    member = _member(team, "co_supervisor")
    return lambda: accounts.change_role(member, Role.DIRECTOR, by=user)


def _extend(team: SimpleNamespace, user: Any) -> Callable[[], Any]:
    from apps.accounts import services as accounts

    member = _member(team, "csi")
    return lambda: accounts.extend_membership(member, by=user)


def _end(team: SimpleNamespace, user: Any) -> Callable[[], Any]:
    from apps.accounts import services as accounts

    member = _member(team, "co_supervisor")
    return lambda: accounts.end_membership(member, by=user)


def h_document(team: SimpleNamespace, level: L = L.PUBLIC) -> Any:
    from apps.vault import services as vault

    return vault.store(
        thesis=team.thesis, upload=_pdf(), title="Note fictive", level=level, user=team.student
    )


CASES: dict[str, tuple[Case, str]] = {
    # name: (case, the role without the right on an ACTIVE thesis)
    "vault.store": (_store, "director"),
    "vault.add_version": (_add_version, "director"),
    "vault.change_level": (_change_level, "director"),
    "vault.grant_access": (_grant, "director"),
    "vault.revoke_access": (_revoke, "director"),
    "vault.sealing": (_sealing, "director"),
    "accounts.create_invitation": (_invite, "director"),
    "accounts.revoke_invitation": (_revoke_invitation, "director"),
    "accounts.change_role": (_change_role, "director"),
    "accounts.extend_membership": (_extend, "director"),
    "accounts.end_membership": (_end, "director"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_closed_thesis_refuses_like_a_missing_right(name: str, time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)
    case, without_right = CASES[name]
    closed_team, active_team = h.make_team(), h.make_team()
    on_closed = case(closed_team, closed_team.student)
    _close(closed_team)
    on_active = case(active_team, getattr(active_team, without_right))
    closed_outcome, active_outcome = _outcome(on_closed), _outcome(on_active)
    assert closed_outcome[0] == "PermissionDenied"
    assert closed_outcome == active_outcome


def test_add_membership_refuses_a_closed_thesis(time_machine: Any) -> None:
    from apps.accounts import services as accounts
    from apps.accounts.tests.factories import UserFactory

    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    _close(team)
    person = UserFactory.create()
    raised, rows = _outcome(
        lambda: accounts.add_membership(
            user=person, thesis=team.thesis, role=Role.CO_SUPERVISOR, organization=_org(team)
        )
    )
    assert (raised, rows) == ("PermissionDenied", [])
    assert not Membership.objects.filter(user=person).exists()


def test_an_invitation_of_a_closed_thesis_looks_expired(time_machine: Any) -> None:
    """Accepting it, or opening its page: the same refusal as for an expired one."""
    import datetime as dt

    from apps.accounts import services as accounts
    from apps.accounts import tasks
    from apps.accounts.models import Invitation
    from apps.accounts.tests.factories import UserFactory

    time_machine.move_to(h.NOW, tick=True)
    outcomes = []
    for spoiled in ("closed", "expired"):
        team = h.make_team()
        person = UserFactory.create(email=f"invite.{spoiled}@example.org")
        inv = accounts.create_invitation(
            thesis=team.thesis, email=person.email, role=Role.CO_SUPERVISOR,
            organization=_org(team), by=team.student,
        )  # fmt: skip
        tasks.send_invitation_email(str(inv.pk))
        if spoiled == "closed":
            _close(team)
        else:
            Invitation.objects.filter(pk=inv.pk).update(expires_at=h.NOW - dt.timedelta(days=1))
        fresh = Invitation.objects.select_related("thesis").get(pk=inv.pk)
        with pytest.raises(accounts.InvalidInvitationError):
            accounts.ensure_usable(fresh)
        outcomes.append(
            _outcome(functools.partial(accounts.accept_invitation, inv.pk, user=person))
        )
        assert not Membership.objects.filter(user=person).exists()
    assert outcomes[0] == outcomes[1] == ("InvalidInvitationError", [])


# --------------------------------------------------------------------------- views


@pytest.mark.parametrize(
    ("name", "kwargs", "data"),
    [
        ("vault:upload", {}, {"title": "Note fictive", "level": "PUBLIC"}),
        (
            "accounts:team_invite",
            {},
            {"email": "invite.fictif@example.org", "role": "CO_SUPERVISOR"},
        ),
    ],
)
def test_the_views_answer_the_same_403(
    name: str, kwargs: dict[str, Any], data: dict[str, Any], time_machine: Any
) -> None:
    from django.urls import reverse

    time_machine.move_to(h.NOW, tick=True)
    closed_team, active_team = h.make_team(), h.make_team()
    _close(closed_team)
    answers = []
    for team, user in ((closed_team, closed_team.student), (active_team, active_team.director)):
        client = h.client_for(user)
        response = client.post(reverse(name, kwargs={"thesis_id": team.thesis.pk, **kwargs}),
                               {**data, "file": _pdf()})  # fmt: skip
        denial = AuditEvent.objects.filter(action="THESIS_ACCESS_DENIED").order_by("-seq").first()
        assert denial is not None
        answers.append((response.status_code, response.content, denial.details_json))
    assert answers[0][0] == 403
    assert answers[0] == answers[1]


# --------------------------------------------------------------------------- M3: right first


def _m3_case(status: str, attr: str) -> tuple[Any, Any, Any]:
    """A submitted CIFRE item validated by the tutor, on a thesis of ``status``."""
    from apps.admin_file.models import ItemStatus, Section
    from apps.admin_file.tests.helpers import item_of, put

    team = h.make_team()
    item = item_of(team, section=Section.CIFRE, validator_roles=[Role.INDUSTRIAL_TUTOR])
    put(item, ItemStatus.SUBMITTED, team.student)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=status)
    return team, item, getattr(team, attr)


# The director sees a CIFRE item without validating it; the co-supervisor does not
# see it; the outsider is no member.
WITHOUT_RIGHT = ("director", "co_supervisor", "outsider")


@pytest.mark.parametrize("operation", ["approve", "reject"])
@pytest.mark.parametrize("attr", WITHOUT_RIGHT)
def test_m3_refuses_whoever_has_no_right_as_on_an_open_thesis(
    attr: str, operation: str, time_machine: Any
) -> None:
    from apps.admin_file import services

    def call(item: Any, user: Any) -> Callable[[], Any]:
        if operation == "approve":
            return lambda: services.approve(item=item, user=user)
        return lambda: services.reject(item=item, user=user, reason="ILLEGIBLE")

    time_machine.move_to(h.NOW, tick=True)
    outcomes = []
    for status in (Thesis.Status.ACTIVE, Thesis.Status.CLOSED):
        _team, item, user = _m3_case(status, attr)
        outcomes.append(_outcome(call(item, user)))
    assert outcomes[0][0] == "PermissionDenied"
    assert outcomes[0] == outcomes[1]


def test_m3_tells_the_validator_only_that_the_file_is_frozen(time_machine: Any) -> None:
    from apps.admin_file import services
    from apps.admin_file.transitions import FrozenFileError

    time_machine.move_to(h.NOW, tick=True)
    _team, item, tutor = _m3_case(Thesis.Status.CLOSED, "tutor")
    with pytest.raises(FrozenFileError):
        services.approve(item=item, user=tutor)


@pytest.mark.parametrize("attr", WITHOUT_RIGHT)
def test_m3_views_answer_the_same(attr: str, time_machine: Any) -> None:
    from django.test import Client
    from django.urls import reverse

    from apps.accounts.tests.helpers import mfa_login

    time_machine.move_to(h.NOW, tick=True)
    answers = []
    for status in (Thesis.Status.ACTIVE, Thesis.Status.CLOSED):
        team, item, user = _m3_case(status, attr)
        client = Client()
        mfa_login(client, user, h.PASSWORD)  # a recent second factor: only the right decides
        url = reverse(
            "admin_file:approve", kwargs={"thesis_id": team.thesis.pk, "item_id": item.pk}
        )
        response = client.post(url)
        answers.append((response.status_code, response.content))
    assert answers[0][0] == 403
    assert answers[0] == answers[1]
