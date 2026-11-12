"""Team mutation views: invite, change role, remove, extend, revoke, resend.

For each: the PhD student succeeds after a recent second factor; every other
role gets 403 (checked before any re-auth prompt); without a recent factor the
student is sent to re-authenticate and nothing changes.
"""

import datetime as dt
import secrets
from collections.abc import Callable
from typing import Any

import pytest
import time_machine
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts import services
from apps.accounts.models import Invitation, Membership, Organization
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory
from apps.accounts.tests.helpers import mfa_login

pytestmark = pytest.mark.django_db

PASSWORD = secrets.token_urlsafe(16)
T0 = dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)
OTHER_ROLES = [r for r in Role if r is not Role.PHD_STUDENT]


@pytest.fixture
def student() -> Membership:
    return MembershipFactory.create(role=Role.PHD_STUDENT, user__password=PASSWORD)


@pytest.fixture
def signed_in(client: Client, student: Membership) -> Client:
    """The student, signed in with password + TOTP at T0 (inside the travel)."""
    with time_machine.travel(T0):
        mfa_login(client, student.user, PASSWORD)
    return client


def _team(m: Membership) -> str:
    return reverse("accounts:team", kwargs={"thesis_id": m.thesis.pk})


def _url(name: str, thesis: Any, **kwargs: Any) -> str:
    return reverse(f"accounts:{name}", kwargs={"thesis_id": thesis.pk, **kwargs})


# Each case: (url builder, POST data, check that the action happened)
Case = tuple[
    Callable[[Membership], str],
    Callable[[Membership], dict[str, Any]],
    Callable[[Membership], bool],
]


def _director(s: Membership) -> Membership:
    return Membership.objects.active().filter(thesis=s.thesis, role=Role.DIRECTOR).first() or (
        MembershipFactory.create(thesis=s.thesis, role=Role.DIRECTOR)
    )


def _csi(s: Membership) -> Membership:
    return Membership.objects.active().filter(thesis=s.thesis, role=Role.CSI_MEMBER).first() or (
        MembershipFactory.create(  # dates relative to T0, the frozen "now" of these tests
            thesis=s.thesis,
            role=Role.CSI_MEMBER,
            active_from=T0 - dt.timedelta(days=1),
            active_until=T0 + dt.timedelta(days=30),
        )
    )


def _open_invitation(s: Membership) -> Invitation:
    inv = Invitation.objects.filter(
        thesis=s.thesis, used_at__isnull=True, revoked_at__isnull=True
    ).first()
    return inv or services.create_invitation(
        thesis=s.thesis,
        email="pending@example.org",
        role=Role.DIRECTOR,
        organization=s.organization,
        by=s.user,
    )


CASES: dict[str, Case] = {
    "invite": (
        lambda s: _url("team_invite", s.thesis),
        lambda s: {
            "email": "new.member@example.org",
            "role": Role.CO_SUPERVISOR,
            "organization": str(s.organization.pk),
        },
        lambda s: Invitation.objects.filter(email="new.member@example.org").exists(),
    ),
    "change_role": (
        lambda s: _url("team_change_role", s.thesis, membership_id=_director(s).pk),
        lambda s: {"role": Role.CO_SUPERVISOR},
        lambda s: (
            Membership.objects.active().filter(thesis=s.thesis, role=Role.CO_SUPERVISOR).exists()
        ),
    ),
    "remove": (
        lambda s: _url("team_remove", s.thesis, membership_id=_director(s).pk),
        lambda s: {},
        lambda s: (
            not Membership.objects.active().filter(thesis=s.thesis, role=Role.DIRECTOR).exists()
        ),
    ),
    "extend": (
        lambda s: _url("team_extend", s.thesis, membership_id=_csi(s).pk),
        lambda s: {"days": 30},
        lambda s: _csi(s).active_until > timezone.now() + dt.timedelta(days=30),  # type: ignore[operator]
    ),
    "resend": (
        lambda s: _url("team_resend_invitation", s.thesis, invitation_id=_open_invitation(s).pk),
        lambda s: {},
        lambda s: Invitation.objects.filter(thesis=s.thesis, revoked_at__isnull=False).exists(),
    ),
}
REAUTH_CASES = list(CASES)


@pytest.mark.parametrize("case", REAUTH_CASES)
def test_student_acts_after_recent_mfa(signed_in: Client, student: Membership, case: str) -> None:
    url_of, data_of, happened = CASES[case]
    with time_machine.travel(T0 + dt.timedelta(minutes=5)):
        url, data = url_of(student), data_of(student)
        response = signed_in.post(url, data)
        assert response.status_code == 302, response.content[:300]
        assert response["Location"] == _team(student)
        assert happened(student)


@pytest.mark.parametrize("case", REAUTH_CASES)
def test_without_recent_mfa_nothing_changes(
    signed_in: Client, student: Membership, case: str
) -> None:
    url_of, data_of, happened = CASES[case]
    with time_machine.travel(T0 + dt.timedelta(minutes=11)):
        url, data = url_of(student), data_of(student)
        response = signed_in.post(
            url, data, headers={"Referer": f"http://testserver{_team(student)}"}
        )
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("mfa_reauthenticate"))
        assert not happened(student)


@pytest.mark.parametrize("role", OTHER_ROLES)
@pytest.mark.parametrize("case", [*REAUTH_CASES, "revoke"])
def test_other_roles_get_403_without_reauth_prompt(
    client: Client, student: Membership, role: Role, case: str
) -> None:
    actor = MembershipFactory.create(thesis=student.thesis, role=role, user__password=PASSWORD)
    if case == "revoke":
        url = _url(
            "team_revoke_invitation", student.thesis, invitation_id=_open_invitation(student).pk
        )
        data: dict[str, Any] = {}
    else:
        url, data = CASES[case][0](student), CASES[case][1](student)
    with time_machine.travel(T0):
        mfa_login(client, actor.user, PASSWORD)
        assert client.post(url, data).status_code == 403


def test_revoke_needs_no_reauth(signed_in: Client, student: Membership) -> None:
    """Revoking reduces access: allowed without a fresh factor (decided in the plan)."""
    inv = _open_invitation(student)
    with time_machine.travel(T0 + dt.timedelta(hours=2)):
        signed_in.force_login(student.user)  # session renewed, no recent factor
        response = signed_in.post(
            _url("team_revoke_invitation", student.thesis, invitation_id=inv.pk)
        )
    assert response.status_code == 302
    inv.refresh_from_db()
    assert inv.revoked_at is not None


def test_mutation_views_refuse_get(signed_in: Client, student: Membership) -> None:
    with time_machine.travel(T0 + dt.timedelta(minutes=1)):
        assert signed_in.get(CASES["invite"][0](student)).status_code == 405


# --------------------------------------------------------------------------- forms and rules


def _post_now(client: Client, url: str, data: dict[str, Any]) -> Any:
    with time_machine.travel(T0 + dt.timedelta(minutes=2)):
        return client.post(url, data)


def test_invite_form_has_no_phd_student_choice(signed_in: Client, student: Membership) -> None:
    with time_machine.travel(T0 + dt.timedelta(minutes=1)):
        html = signed_in.get(_team(student)).content.decode()
    assert 'name="role"' in html
    assert 'value="PHD_STUDENT"' not in html


def test_inviting_phd_student_is_refused(signed_in: Client, student: Membership) -> None:
    response = _post_now(
        signed_in,
        _url("team_invite", student.thesis),
        {
            "email": "x@example.org",
            "role": Role.PHD_STUDENT,
            "organization": str(student.organization.pk),
        },
    )
    assert response.status_code == 400
    assert not Invitation.objects.filter(email="x@example.org").exists()


def test_invite_with_a_new_organization(signed_in: Client, student: Membership) -> None:
    response = _post_now(
        signed_in,
        _url("team_invite", student.thesis),
        {
            "email": "csi@example.org",
            "role": Role.CSI_MEMBER,
            "new_organization": "Université fictive",
            "new_organization_kind": Organization.Kind.UNIVERSITY,
        },
    )
    assert response.status_code == 302
    inv = Invitation.objects.get(email="csi@example.org")
    assert inv.organization.name == "Université fictive"


def test_invite_needs_exactly_one_organization(signed_in: Client, student: Membership) -> None:
    for extra in ({}, {"organization": str(student.organization.pk), "new_organization": "Autre"}):
        response = _post_now(
            signed_in,
            _url("team_invite", student.thesis),
            {
                "email": "y@example.org",
                "role": Role.DIRECTOR,
                **extra,
                "new_organization_kind": "EXTERNAL",
            },
        )
        assert response.status_code == 400


def test_invite_queues_the_email(
    signed_in: Client, student: Membership, django_capture_on_commit_callbacks: Any
) -> None:
    mail.outbox.clear()
    url_of, data_of, _ = CASES["invite"]
    with django_capture_on_commit_callbacks(execute=True):
        _post_now(signed_in, url_of(student), data_of(student))
    assert [m.to for m in mail.outbox] == [["new.member@example.org"]]


@pytest.mark.parametrize("days", [0, 91, "abc"])
def test_extend_rejects_out_of_range(signed_in: Client, student: Membership, days: Any) -> None:
    csi = _csi(student)
    before = csi.active_until
    response = _post_now(
        signed_in, _url("team_extend", student.thesis, membership_id=csi.pk), {"days": days}
    )
    assert response.status_code == 302  # back to the team page, with an error message
    csi.refresh_from_db()
    assert csi.active_until == before


def test_extend_defaults_to_60_days(signed_in: Client, student: Membership) -> None:
    csi = _csi(student)
    before = csi.active_until
    _post_now(signed_in, _url("team_extend", student.thesis, membership_id=csi.pk), {})
    csi.refresh_from_db()
    assert csi.active_until == before + dt.timedelta(days=60)  # type: ignore[operator]


def test_membership_of_another_thesis_gives_404(signed_in: Client, student: Membership) -> None:
    foreign = MembershipFactory.create(role=Role.DIRECTOR)
    response = _post_now(
        signed_in, _url("team_remove", student.thesis, membership_id=foreign.pk), {}
    )
    assert response.status_code == 404
    assert Membership.objects.active().filter(pk=foreign.pk).exists()


def test_rule_violation_is_reported_not_crashed(signed_in: Client, student: Membership) -> None:
    response = _post_now(
        signed_in, _url("team_remove", student.thesis, membership_id=student.pk), {}
    )
    assert response.status_code == 302
    with time_machine.travel(T0 + dt.timedelta(minutes=2)):
        html = signed_in.get(_team(student)).content.decode()
    assert "ne peut pas être retiré" in html


def test_team_page_offers_actions_to_student_only(
    client: Client, signed_in: Client, student: Membership
) -> None:
    _director(student)
    with time_machine.travel(T0 + dt.timedelta(minutes=1)):
        html = signed_in.get(_team(student)).content.decode()
    assert "team/invite/" in html and "/remove/" in html
    director = _director(student)
    other = Client()
    other.force_login(director.user)
    html = other.get(_team(student)).content.decode()
    assert "team/invite/" not in html and "/remove/" not in html
