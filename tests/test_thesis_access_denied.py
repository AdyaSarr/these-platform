"""THESIS_ACCESS_DENIED: every refusal of the thesis guard is audited, for signed-in
users only (docs/ROADMAP.md, "Prochaine étape").

- IDOR: a signed-in user of another thesis asking for a page of this thesis, or of
  a thesis that does not exist, gets the same 403 and leaves an event with their
  identity; the response never tells whether the thesis exists.
- A member lacking the action (GUEST, CSI member on the team page) is audited too
  (reason ``missing_right``).
- An anonymous visitor is sent to the login page and leaves NO event: the log is
  append-only and must not be filled by unauthenticated traffic.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from django.views import View

from apps.accounts.models import Membership, Thesis
from apps.accounts.permissions import (
    Action,
    ThesisPermissionRequiredMixin,
    thesis_permission_required,
)
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db

DENIED = "THESIS_ACCESS_DENIED"


def _denials() -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=DENIED).order_by("seq"))


def _team_url(thesis_id: Any) -> str:
    return reverse("accounts:team", kwargs={"thesis_id": thesis_id})


# --------------------------------------------------------------------------- HTTP (IDOR)


def test_user_of_another_thesis_gets_403_and_an_event(client: Client) -> None:
    intruder = MembershipFactory.create(role=Role.DIRECTOR)
    target = ThesisFactory.create()
    client.force_login(intruder.user)

    response = client.get(_team_url(target.pk))

    assert response.status_code == 403
    assert target.title not in response.content.decode()
    [event] = _denials()
    assert event.actor_id == intruder.user.pk
    assert event.thesis_id == target.pk
    assert event.details_json == {"operation": "VIEW_TEAM", "reason": "not_member"}


def test_unknown_thesis_gets_the_same_403_and_an_event(client: Client) -> None:
    intruder = MembershipFactory.create(role=Role.DIRECTOR)
    target = ThesisFactory.create()
    missing = uuid.uuid4()
    client.force_login(intruder.user)

    existing = client.get(_team_url(target.pk))
    unknown = client.get(_team_url(missing))

    assert unknown.status_code == existing.status_code == 403
    assert unknown.content == existing.content  # nothing tells the two apart
    first, second = _denials()
    assert first.thesis_id == target.pk
    assert second.actor_id == intruder.user.pk
    assert second.thesis_id is None
    assert second.details_json == {
        "operation": "VIEW_TEAM",
        "reason": "unknown_thesis",
        "requested_thesis_id": str(missing),
    }


def test_removed_member_is_audited_as_not_member(client: Client) -> None:
    m = MembershipFactory.create(role=Role.DIRECTOR)
    Membership.objects.filter(pk=m.pk).update(
        active_from=timezone.now() - dt.timedelta(days=2), active_until=timezone.now()
    )
    client.force_login(m.user)

    assert client.get(_team_url(m.thesis.pk)).status_code == 403
    [event] = _denials()
    assert event.thesis_id == m.thesis.pk
    assert event.details_json["reason"] == "not_member"


@pytest.mark.parametrize(
    ("role", "url_name"),
    [(Role.GUEST, "dashboard"), (Role.CSI_MEMBER, "team")],
)
def test_member_without_the_action_is_audited_as_missing_right(
    client: Client, role: Role, url_name: str
) -> None:
    m = MembershipFactory.create(role=role)
    client.force_login(m.user)

    response = client.get(reverse(f"accounts:{url_name}", kwargs={"thesis_id": m.thesis.pk}))

    assert response.status_code == 403
    [event] = _denials()
    assert event.actor_id == m.user.pk
    assert event.thesis_id == m.thesis.pk
    assert event.details_json["reason"] == "missing_right"


def test_anonymous_is_redirected_without_any_event(client: Client) -> None:
    target = ThesisFactory.create()
    before = AuditEvent.objects.count()

    for thesis_id in (target.pk, uuid.uuid4()):
        response = client.get(_team_url(thesis_id))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("account_login"))

    assert AuditEvent.objects.count() == before


def test_allowed_access_leaves_no_denial(client: Client) -> None:
    m = MembershipFactory.create(role=Role.DIRECTOR)
    client.force_login(m.user)
    assert client.get(_team_url(m.thesis.pk)).status_code == 200
    assert _denials() == []


def test_the_event_carries_the_ip_pseudonym(client: Client) -> None:
    intruder = MembershipFactory.create(role=Role.DIRECTOR)
    client.force_login(intruder.user)
    client.get(_team_url(uuid.uuid4()), REMOTE_ADDR="192.0.2.7")
    [event] = _denials()
    assert len(event.ip_hash) == 64
    assert "192.0.2.7" not in str(event.details_json)


# --------------------------------------------------------------------------- both guards


class _TeamView(ThesisPermissionRequiredMixin, View):
    required_action = Action.VIEW_TEAM

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return HttpResponse("ok")


@thesis_permission_required(Action.VIEW_TEAM)
def _team_function(request: HttpRequest, thesis: Thesis) -> HttpResponse:
    return HttpResponse("ok")


_VIEWS = [_TeamView.as_view(), _team_function]


def _call(user: Any, view: Any, thesis_id: Any) -> HttpResponse:
    request = RequestFactory().get("/t/x/")
    request.user = user
    response: HttpResponse = view(request, thesis_id=thesis_id)
    return response


@pytest.mark.parametrize("view", _VIEWS, ids=["mixin", "decorator"])
def test_both_guards_audit_a_foreign_thesis(view: Any) -> None:
    intruder = MembershipFactory.create(role=Role.DIRECTOR)
    target = ThesisFactory.create()
    with pytest.raises(PermissionDenied):
        _call(intruder.user, view, target.pk)
    [event] = _denials()
    assert (event.actor_id, event.thesis_id) == (intruder.user.pk, target.pk)


@pytest.mark.parametrize("view", _VIEWS, ids=["mixin", "decorator"])
@pytest.mark.parametrize("bad_id", ["not-a-uuid", None, ""])
def test_malformed_id_is_audited_without_echoing_it(view: Any, bad_id: Any) -> None:
    """A malformed id is attacker-chosen text: it is never copied into the log."""
    intruder = MembershipFactory.create(role=Role.DIRECTOR)
    with pytest.raises(PermissionDenied):
        _call(intruder.user, view, bad_id)
    [event] = _denials()
    assert event.thesis_id is None
    assert event.details_json == {"operation": "VIEW_TEAM", "reason": "unknown_thesis"}


@pytest.mark.parametrize("view", _VIEWS, ids=["mixin", "decorator"])
def test_both_guards_stay_silent_for_anonymous(view: Any) -> None:
    target = ThesisFactory.create()
    response = _call(AnonymousUser(), view, target.pk)
    assert response.status_code == 302
    assert not AuditEvent.objects.exists()
