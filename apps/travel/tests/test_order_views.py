"""Views of the mission orders: request and withdrawal by the student, decision by the
payer's person (403 before the second factor for anyone else), the decider's queue,
POSTs like a browser."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from django.urls import reverse
from django.views import View

from apps.accounts.permissions import ThesisPermissionRequiredMixin
from apps.accounts.reauth import RecentAuthRequiredMixin
from apps.travel import services, views
from apps.travel.models import Mission, MissionOrder
from apps.travel.tests.helpers import (
    NOW,
    ROLES,
    browser_for,
    browser_post,
    client_for,
    make_team,
    mission_of,
    url,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _m(team: SimpleNamespace, mission: Mission, name: str = "mission") -> str:
    return url(team, name, mission_id=mission.pk)


def _o(team: SimpleNamespace, order: MissionOrder, name: str) -> str:
    return url(team, name, order_id=order.pk)


def _requested(team: SimpleNamespace, payer: str = "ST") -> MissionOrder:
    mission = mission_of(team, payer=payer)
    return services.request_order(mission=mission, user=team.student, estimated_total="250")


def test_decide_view_checks_rights_before_the_second_factor() -> None:
    mro = views.DecideOrderView.__mro__
    assert (
        mro.index(ThesisPermissionRequiredMixin)
        < mro.index(views._DeciderGuard)  # the payer's person, after self.thesis is set
        < mro.index(RecentAuthRequiredMixin)
        < mro.index(View)
    )


@pytest.mark.parametrize("role", ROLES)
def test_queue_per_role(team: SimpleNamespace, role: str) -> None:
    expected = 200 if role in ("director", "tutor") else 403
    assert client_for(team, role).get(url(team, "orders")).status_code == expected


def test_the_student_requests_like_a_browser(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    page = client_for(team, "student").get(_m(team, mission)).content.decode()
    assert 'name="estimated_total"' in page
    client = browser_for(team, "student")
    response = browser_post(
        client, _m(team, mission), _m(team, mission, "request_order"), {"estimated_total": "312,40"}
    )
    assert response.status_code == 302, response.content[:500]
    (view,) = services.orders_of(team.student, mission)
    assert view.estimate == 31240


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor"])
def test_only_the_student_requests(team: SimpleNamespace, role: str) -> None:
    mission = mission_of(team)
    response = client_for(team, role).post(
        _m(team, mission, "request_order"), {"estimated_total": "10"}
    )
    assert response.status_code == 403
    assert not MissionOrder.objects.exists()


def test_an_invalid_estimate_is_a_400(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    response = client_for(team, "student").post(
        _m(team, mission, "request_order"), {"estimated_total": "1e3"}
    )
    assert response.status_code == 400
    assert not MissionOrder.objects.exists()


def test_a_second_request_is_a_409(team: SimpleNamespace) -> None:
    order = _requested(team)
    mission = Mission.objects.get(pk=order.mission_id)
    response = client_for(team, "student").post(
        _m(team, mission, "request_order"), {"estimated_total": "10"}
    )
    assert response.status_code == 409


def test_only_the_decider_sees_the_decision_form(team: SimpleNamespace) -> None:
    order = _requested(team, payer="ST")
    mission = Mission.objects.get(pk=order.mission_id)
    for role in ("student", "director", "co_supervisor", "tutor"):
        html = client_for(team, role).get(_m(team, mission)).content.decode()
        assert ('name="decision"' in html) is (role == "tutor"), role


def test_decide_like_a_browser(team: SimpleNamespace) -> None:
    order = _requested(team, payer="ST")
    mission = Mission.objects.get(pk=order.mission_id)
    target = _o(team, order, "decide_order")
    data = {"decision": "GRANTED", "reason": "", "comment": ""}
    for role in ("student", "co_supervisor"):
        assert client_for(team, role).post(target, data).status_code == 403
    # The director holds the right but is not ST's person: refused by the service, no factor.
    assert client_for(team, "director").post(target, data).status_code == 403
    response = client_for(team, "tutor").post(target, data)
    assert response.status_code == 302 and reverse("mfa_reauthenticate") in response["Location"]
    order.refresh_from_db()
    assert order.decision == "PENDING"
    client = browser_for(team, "tutor", mfa=True)
    response = browser_post(client, _m(team, mission), target, data)
    assert response.status_code == 302, response.content[:500]
    order.refresh_from_db()
    assert order.decision == "GRANTED"


def test_a_refusal_without_reason_is_a_400(team: SimpleNamespace) -> None:
    order = _requested(team, payer="LIRMM")
    response = client_for(team, "director", mfa=True).post(
        _o(team, order, "decide_order"), {"decision": "REFUSED", "reason": "", "comment": ""}
    )
    assert response.status_code == 400
    order.refresh_from_db()
    assert order.decision == "PENDING"


def test_a_decided_order_is_a_409(team: SimpleNamespace) -> None:
    order = _requested(team, payer="LIRMM")
    services.decide_order(order=order, user=team.director, decision="GRANTED", recent_mfa=True)
    response = client_for(team, "director", mfa=True).post(
        _o(team, order, "decide_order"),
        {"decision": "REFUSED", "reason": "OTHER", "comment": ""},
    )
    assert response.status_code == 409


def test_withdraw_like_a_browser(team: SimpleNamespace) -> None:
    order = _requested(team)
    mission = Mission.objects.get(pk=order.mission_id)
    assert client_for(team, "tutor").post(_o(team, order, "withdraw_order")).status_code == 403
    client = browser_for(team, "student")
    response = browser_post(client, _m(team, mission), _o(team, order, "withdraw_order"), {})
    assert response.status_code == 302
    order.refresh_from_db()
    assert order.decision == "WITHDRAWN"


def test_an_order_of_another_thesis_or_unknown_is_refused(team: SimpleNamespace) -> None:
    other = make_team()
    theirs = _requested(other)
    client = client_for(team, "tutor", mfa=True)
    data = {"decision": "GRANTED", "reason": "", "comment": ""}
    for order_id in (theirs.pk, uuid.uuid4()):
        response = client.post(url(team, "decide_order", order_id=order_id), data)
        assert response.status_code == 403
    theirs.refresh_from_db()
    assert theirs.decision == "PENDING"


def test_the_queue_lists_what_the_decider_decides(team: SimpleNamespace) -> None:
    st, lab = _requested(team, payer="ST"), _requested(team, payer="LIRMM")
    tutor = client_for(team, "tutor").get(url(team, "orders")).content.decode()
    director = client_for(team, "director").get(url(team, "orders")).content.decode()
    assert str(st.mission_id) in tutor and str(lab.mission_id) not in tutor
    assert str(lab.mission_id) in director and str(st.mission_id) not in director
