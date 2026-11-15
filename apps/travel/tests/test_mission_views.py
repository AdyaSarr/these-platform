"""Views of the missions (/t/<thesis_id>/travel/missions/): rights per role, POSTs like
a browser, the second factor for the level, a hidden mission answered like an
unknown one, restricted texts, the no-address warning."""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from django.urls import reverse
from django.views import View

from apps.accounts.permissions import ThesisPermissionRequiredMixin
from apps.accounts.reauth import RecentAuthRequiredMixin
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.travel import views
from apps.travel.models import Mission, PresenceDay
from apps.travel.tests.helpers import (
    NOW,
    READERS,
    ROLES,
    browser_for,
    browser_post,
    client_for,
    make_team,
    marker,
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


FORM = {
    "start_date": "2027-02-08",
    "end_date": "2027-02-10",
    "destination": "LIRMM",
    "payer": "LIRMM",
    "level": "INTERNAL_LIRMM",
    "transports": ["TRAIN", "URBAN_TRANSIT"],
    "lodging": "GUESTHOUSE",
    "destination_detail": "",
    "purpose": "Séjour fictif.",
    "lodging_note": "",
    "fill_presence": "on",
}


# =========================================================================== rights


@pytest.mark.parametrize("role", ROLES)
def test_list_per_role(team: SimpleNamespace, role: str) -> None:
    assert client_for(team, role).get(url(team, "missions")).status_code == (
        200 if role in READERS else 403
    )


@pytest.mark.parametrize("role", ROLES)
def test_new_mission_page_is_the_students(team: SimpleNamespace, role: str) -> None:
    assert client_for(team, role).get(url(team, "new_mission")).status_code == (
        200 if role == "student" else 403
    )


@pytest.mark.parametrize("role", ROLES)
def test_detail_per_role(team: SimpleNamespace, role: str) -> None:
    mission = mission_of(team)  # to the LIRMM: the co-supervisor sees it
    assert client_for(team, role).get(_m(team, mission)).status_code == (
        200 if role in READERS else 403
    )


@pytest.mark.parametrize("role", ROLES)
def test_edit_page_is_the_students(team: SimpleNamespace, role: str) -> None:
    mission = mission_of(team)
    assert client_for(team, role).get(_m(team, mission, "edit_mission")).status_code == (
        200 if role == "student" else 403
    )


def test_a_hidden_mission_is_answered_like_an_unknown_one(team: SimpleNamespace) -> None:
    hidden = mission_of(team, destination="ROUSSET")
    client = client_for(team, "co_supervisor")
    seen = client.get(_m(team, hidden))
    unknown = client.get(url(team, "mission", mission_id=uuid.uuid4()))
    assert seen.status_code == unknown.status_code == 403
    assert seen.content == unknown.content
    assert client_for(team, "director").get(_m(team, hidden)).status_code == 200


def test_a_mission_of_another_thesis_is_refused(team: SimpleNamespace) -> None:
    theirs = mission_of(make_team())
    assert client_for(team, "student").get(_m(team, theirs)).status_code == 403


@pytest.mark.parametrize("view", [views.MissionLevelView], ids=lambda v: v.__name__)
def test_rights_are_checked_before_the_second_factor(view: Any) -> None:
    mro = view.__mro__
    assert (
        mro.index(ThesisPermissionRequiredMixin)
        < mro.index(RecentAuthRequiredMixin)
        < mro.index(View)
    )


# =========================================================================== forms


def test_the_student_plans_a_mission_like_a_browser(team: SimpleNamespace) -> None:
    client = browser_for(team, "student")
    response = browser_post(client, url(team, "new_mission"), url(team, "new_mission"), FORM)
    assert response.status_code == 302, response.content[:600]
    mission = Mission.objects.get()
    assert response["Location"] == _m(team, mission)
    assert (mission.payer, mission.level, mission.lodging) == (
        "LIRMM",
        "INTERNAL_LIRMM",
        "GUESTHOUSE",
    )
    assert mission.transports == ["TRAIN", "URBAN_TRANSIT"]
    assert PresenceDay.objects.filter(thesis=team.thesis, kind="LIRMM").count() == 3


def test_the_form_warns_against_any_address(team: SimpleNamespace) -> None:
    html = client_for(team, "student").get(url(team, "new_mission")).content.decode()
    assert "adresse" in html


def test_the_form_offers_no_car(team: SimpleNamespace) -> None:
    html = client_for(team, "student").get(url(team, "new_mission")).content.decode()
    assert 'value="TRAIN"' in html and 'value="CAR"' not in html


@pytest.mark.parametrize(
    "change",
    [
        {"end_date": "2027-02-01"},
        {"payer": "ST", "level": "INTERNAL_LIRMM"},
        {"transports": []},
        {"destination": "OTHER"},
    ],
)
def test_an_invalid_mission_is_a_400(team: SimpleNamespace, change: dict[str, Any]) -> None:
    response = client_for(team, "student").post(url(team, "new_mission"), {**FORM, **change})
    assert response.status_code == 400
    assert not Mission.objects.exists()


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor"])
def test_only_the_student_posts_a_mission(team: SimpleNamespace, role: str) -> None:
    assert client_for(team, role).post(url(team, "new_mission"), FORM).status_code == 403
    assert not Mission.objects.exists()


def test_edit_like_a_browser(team: SimpleNamespace) -> None:
    mission = mission_of(team, payer="LIRMM")
    client = browser_for(team, "student")
    data = {**FORM, "end_date": "2027-02-12", "purpose": "Motif modifié fictif."}
    response = browser_post(
        client, _m(team, mission, "edit_mission"), _m(team, mission, "edit_mission"), data
    )
    assert response.status_code == 302, response.content[:600]
    mission.refresh_from_db()
    assert mission.end_date == dt.date(2027, 2, 12)


def test_level_change_asks_for_the_second_factor(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    target = _m(team, mission, "mission_level")
    assert client_for(team, "tutor").post(target, {"level": "PUBLIC"}).status_code == 403
    response = client_for(team, "student").post(target, {"level": "PUBLIC"})
    assert response.status_code == 302 and reverse("mfa_reauthenticate") in response["Location"]
    client = browser_for(team, "student", mfa=True)
    response = browser_post(client, _m(team, mission), target, {"level": "CONFIDENTIAL"})
    assert response.status_code == 302, response.content[:600]
    mission.refresh_from_db()
    assert mission.level == L.CONFIDENTIAL.value


def test_cancel_like_a_browser(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    assert client_for(team, "director").post(_m(team, mission, "cancel_mission")).status_code == 403
    client = browser_for(team, "student")
    response = browser_post(client, _m(team, mission), _m(team, mission, "cancel_mission"), {})
    assert response.status_code == 302
    mission.refresh_from_db()
    assert mission.status == "CANCELLED"


def test_done_too_early_is_a_409(team: SimpleNamespace) -> None:
    mission = mission_of(team, payer="OTHER")
    response = client_for(team, "student").post(_m(team, mission, "mission_done"))
    assert response.status_code == 409


# =========================================================================== content


def test_restricted_texts_are_shown_as_such(team: SimpleNamespace) -> None:
    secret = marker()
    mission = mission_of(team, payer="LIRMM", purpose=secret)  # INTERNAL_LIRMM
    tutor = client_for(team, "tutor").get(_m(team, mission)).content.decode()
    assert secret not in tutor and "Contenu restreint" in tutor
    assert secret in client_for(team, "co_supervisor").get(_m(team, mission)).content.decode()


def test_the_list_shows_no_text(team: SimpleNamespace) -> None:
    secret = marker()
    mission_of(team, purpose=secret)
    html = client_for(team, "student").get(url(team, "missions")).content.decode()
    assert "08/02/2027" in html and secret not in html
