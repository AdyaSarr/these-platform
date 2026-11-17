"""Home, dashboard (one block per action), team page, CSI file: allowed/refused per role."""

import datetime as dt
import re

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership
from apps.accounts.permissions import ROLE_ACTIONS
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, UserFactory
from apps.accounts.views import DASHBOARD_BLOCKS

pytestmark = pytest.mark.django_db

SUPERVISION = {Role.PHD_STUDENT, Role.DIRECTOR, Role.CO_SUPERVISOR, Role.INDUSTRIAL_TUTOR}


def _login(client: Client, role: Role) -> Membership:
    m = MembershipFactory.create(role=role)
    client.force_login(m.user)
    return m


def _url(name: str, m: Membership) -> str:
    return reverse(f"accounts:{name}", kwargs={"thesis_id": m.thesis.pk})


# --------------------------------------------------------------------------- home


def test_home_without_thesis_lists_nothing(client: Client) -> None:
    MembershipFactory.create()  # someone else's thesis
    client.force_login(UserFactory.create())
    response = client.get(reverse("core:home"))
    assert response.status_code == 200
    assert list(response.context["theses"]) == []


def test_home_with_one_thesis_redirects_to_it(client: Client) -> None:
    m = _login(client, Role.DIRECTOR)
    response = client.get(reverse("core:home"))
    assert response.status_code == 302
    assert response["Location"] == _url("dashboard", m)


def test_home_lists_only_own_theses(client: Client) -> None:
    m = _login(client, Role.DIRECTOR)
    second = MembershipFactory.create(user=m.user, role=Role.CO_SUPERVISOR)
    foreign = MembershipFactory.create()
    response = client.get(reverse("core:home"))
    assert response.status_code == 200
    assert set(response.context["theses"]) == {m.thesis, second.thesis}
    assert foreign.thesis.title not in response.content.decode()


def test_home_ignores_ended_membership(client: Client) -> None:
    m = MembershipFactory.create(
        active_from=timezone.now() - dt.timedelta(days=2),
        active_until=timezone.now() - dt.timedelta(days=1),
    )
    client.force_login(m.user)
    response = client.get(reverse("core:home"))
    assert response.status_code == 200
    assert m.thesis.title not in response.content.decode()


# --------------------------------------------------------------------------- dashboard


@pytest.mark.parametrize("role", sorted(SUPERVISION))
def test_dashboard_allowed_for_supervision(client: Client, role: Role) -> None:
    m = _login(client, role)
    response = client.get(_url("dashboard", m))
    assert response.status_code == 200
    assert m.thesis.title in response.content.decode()


def test_dashboard_redirects_csi_member_to_csi_file(client: Client) -> None:
    m = _login(client, Role.CSI_MEMBER)
    response = client.get(_url("dashboard", m))
    assert response.status_code == 302
    assert response["Location"] == _url("csi_file", m)


def test_dashboard_refused_for_guest(client: Client) -> None:
    m = _login(client, Role.GUEST)
    assert client.get(_url("dashboard", m)).status_code == 403


def test_dashboard_refused_for_non_member(client: Client) -> None:
    m = MembershipFactory.create()
    client.force_login(UserFactory.create())
    assert client.get(_url("dashboard", m)).status_code == 403


def test_dashboard_requires_login(client: Client) -> None:
    m = MembershipFactory.create()
    response = client.get(_url("dashboard", m))
    assert response.status_code == 302
    assert response["Location"].startswith("/accounts/login/")


def _rendered_blocks(html: str) -> set[str]:
    return set(re.findall(r'data-block="([a-z_]+)"', html))


@pytest.mark.parametrize("role", sorted(SUPERVISION))
def test_dashboard_shows_exactly_the_blocks_the_role_may_see(client: Client, role: Role) -> None:
    m = _login(client, role)
    html = client.get(_url("dashboard", m)).content.decode()
    # card=False: a block that only feeds the tiles (frontend step 8a).
    expected = {b.key for b in DASHBOARD_BLOCKS if b.action in ROLE_ACTIONS[role] and b.card}
    assert _rendered_blocks(html) == expected


def test_every_block_is_guarded_by_an_action() -> None:
    keys = [b.key for b in DASHBOARD_BLOCKS]
    assert len(keys) == len(set(keys))
    assert all(b.action is not None for b in DASHBOARD_BLOCKS)


def test_co_supervisor_does_not_get_admin_block(client: Client) -> None:
    """Concrete instance of the rule above: admin. file is '—' for co-supervisors."""
    m = _login(client, Role.CO_SUPERVISOR)
    html = client.get(_url("dashboard", m)).content.decode()
    assert 'data-block="admin_file"' not in html
    assert 'data-block="team"' in html


# --------------------------------------------------------------------------- team


@pytest.mark.parametrize("role", sorted(SUPERVISION))
def test_team_allowed_for_supervision(client: Client, role: Role) -> None:
    m = _login(client, role)
    assert client.get(_url("team", m)).status_code == 200


@pytest.mark.parametrize("role", [Role.CSI_MEMBER, Role.GUEST])
def test_team_refused_for_csi_and_guest(client: Client, role: Role) -> None:
    m = _login(client, role)
    assert client.get(_url("team", m)).status_code == 403


def test_team_lists_active_members_of_this_thesis_only(client: Client) -> None:
    m = _login(client, Role.PHD_STUDENT)
    director = MembershipFactory.create(thesis=m.thesis, role=Role.DIRECTOR)
    gone = MembershipFactory.create(
        thesis=m.thesis,
        role=Role.CO_SUPERVISOR,
        active_from=timezone.now() - dt.timedelta(days=2),
        active_until=timezone.now() - dt.timedelta(days=1),
    )
    foreign = MembershipFactory.create(role=Role.DIRECTOR)
    response = client.get(_url("team", m))
    members = set(response.context["memberships"])
    assert members == {m, director}
    html = response.content.decode()
    assert director.user.email in html
    assert gone.user.email not in html
    assert foreign.user.email not in html


def test_team_shows_csi_end_date(client: Client) -> None:
    m = _login(client, Role.PHD_STUDENT)
    MembershipFactory.create(
        thesis=m.thesis, role=Role.CSI_MEMBER, active_until=timezone.now() + dt.timedelta(days=60)
    )
    html = client.get(_url("team", m)).content.decode()
    assert 'data-until="' in html


# --------------------------------------------------------------------------- CSI file


def test_csi_file_allowed_for_csi_member(client: Client) -> None:
    m = _login(client, Role.CSI_MEMBER)
    assert client.get(_url("csi_file", m)).status_code == 200


@pytest.mark.parametrize("role", sorted(set(Role) - {Role.CSI_MEMBER}))
def test_csi_file_refused_for_other_roles(client: Client, role: Role) -> None:
    m = _login(client, role)
    assert client.get(_url("csi_file", m)).status_code == 403


def test_expired_csi_member_gets_403(client: Client) -> None:
    m = MembershipFactory.create(
        role=Role.CSI_MEMBER,
        active_from=timezone.now() - dt.timedelta(days=61),
        active_until=timezone.now() - dt.timedelta(seconds=1),
    )
    client.force_login(m.user)
    assert client.get(_url("csi_file", m)).status_code == 403
    assert client.get(_url("dashboard", m)).status_code == 403


def test_pages_are_in_french(client: Client) -> None:
    m = _login(client, Role.PHD_STUDENT)
    dashboard = client.get(_url("dashboard", m)).content.decode()
    team = client.get(_url("team", m)).content.decode()
    assert "Voir l&#x27;équipe" in dashboard or "Voir l'équipe" in dashboard
    assert "Doctorant·e" in team
    assert "Adresse électronique" in team
