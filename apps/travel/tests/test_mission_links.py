"""Links of a mission to a meeting (M5) or a submission (M6), in ?meeting= / ?submission=
of the new-mission page and in its POST: an id that is malformed, unknown, of another
thesis, or of an object the user may not read is refused ALIKE (the same page, the same
message, the same journal entry), and no title is ever shown."""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from django.urls import reverse

from apps.conferences.tests.helpers import submission_of
from apps.meetings.tests.helpers import meeting_of
from apps.travel import services
from apps.travel.models import Mission
from apps.travel.tests.helpers import NOW, client_for, make_team, marker, url
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)

KINDS = ("meeting", "submission")
FORM = {
    "start_date": "2027-02-08",
    "end_date": "2027-02-10",
    "destination": "LIRMM",
    "payer": "ST",
    "level": "INTERNAL_ST",
    "transports": ["TRAIN"],
    "lodging": "NONE",
    "purpose": "",
}
_CSRF = re.compile(r'(name="csrfmiddlewaretoken" value=|"X-CSRFToken": )"[^"]+"')


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=False)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _object(team: SimpleNamespace, kind: str, **kw: Any) -> Any:
    return meeting_of(team, **kw) if kind == "meeting" else submission_of(team, **kw)


def _refused_ids(team: SimpleNamespace, kind: str) -> dict[str, str]:
    """case -> raw id; the title of the other thesis's object is a marker."""
    other = make_team()
    theirs = _object(other, kind, title=marker())
    return {
        "malformed": "pas-un-identifiant",
        "unknown": str(uuid.uuid4()),
        "other_thesis": str(theirs.pk),
    }


def _denials() -> list[dict[str, Any]]:
    return [
        e.details_json
        for e in AuditEvent.objects.filter(action="TRAVEL_ACCESS_DENIED").order_by("seq")
    ]


def _body(response: Any) -> str:
    return _CSRF.sub(r'\1"-"', response.content.decode())


# =========================================================================== service


@pytest.mark.parametrize("kind", KINDS)
def test_a_readable_object_of_the_thesis_is_linked(team: SimpleNamespace, kind: str) -> None:
    obj = _object(team, kind)
    assert services.link_for(team.student, team.thesis, kind, str(obj.pk)).pk == obj.pk
    assert _denials() == []


@pytest.mark.parametrize("kind", KINDS)
def test_refusals_are_alike_and_journaled(team: SimpleNamespace, kind: str) -> None:
    raw_ids = _refused_ids(team, kind)
    for raw in raw_ids.values():
        assert services.link_for(team.student, team.thesis, kind, raw) is None
    denials = _denials()
    assert len(denials) == 3
    assert all(d == denials[0] for d in denials)  # nothing tells the cases apart
    assert denials[0]["reason"] == "unknown_link" and denials[0]["kind"] == kind


@pytest.mark.parametrize("kind", KINDS)
def test_an_object_the_user_may_not_read_is_refused_alike(team: SimpleNamespace, kind: str) -> None:
    obj = _object(team, kind)
    services.link_for(team.student, team.thesis, kind, str(uuid.uuid4()))
    assert services.link_for(team.csi, team.thesis, kind, str(obj.pk)) is None
    student_denial, csi_denial = _denials()
    assert csi_denial == student_denial


def test_no_id_is_no_link_and_no_refusal(team: SimpleNamespace) -> None:
    assert services.link_for(team.student, team.thesis, "meeting", "") is None
    assert _denials() == []


# =========================================================================== prefill


@pytest.mark.parametrize("kind", KINDS)
def test_a_refused_prefill_is_the_plain_page(team: SimpleNamespace, kind: str) -> None:
    client = client_for(team, "student")
    plain = _body(client.get(url(team, "new_mission")))
    for case, raw in _refused_ids(team, kind).items():
        response = client.get(url(team, "new_mission") + f"?{kind}={raw}")
        assert response.status_code == 200, case
        assert _body(response) == plain, case  # no prefill, no title, no hint
        assert "TEMOIN-" not in _body(response)


@pytest.mark.parametrize("kind", KINDS)
def test_a_valid_prefill_shows_no_title(team: SimpleNamespace, kind: str) -> None:
    secret = marker()
    obj = _object(team, kind, title=secret)
    html = client_for(team, "student").get(url(team, "new_mission") + f"?{kind}={obj.pk}")
    body = html.content.decode()
    assert f'name="{kind}" value="{obj.pk}"' in body
    assert secret not in body


# =========================================================================== POST


@pytest.mark.parametrize("kind", KINDS)
def test_a_refused_link_in_a_post_is_refused_alike(team: SimpleNamespace, kind: str) -> None:
    client = client_for(team, "student")
    bodies = {}
    for case, raw in _refused_ids(team, kind).items():
        response = client.post(url(team, "new_mission"), {**FORM, kind: raw})
        assert response.status_code == 400, case
        bodies[case] = _body(response)
    assert len(set(bodies.values())) == 1, sorted(bodies)
    assert "Objet lié inconnu" in bodies["unknown"]
    assert not Mission.objects.exists()
    assert len(_denials()) == 3


@pytest.mark.parametrize("kind", KINDS)
def test_a_valid_link_in_a_post(team: SimpleNamespace, kind: str) -> None:
    obj = _object(team, kind)
    response = client_for(team, "student").post(
        url(team, "new_mission"), {**FORM, kind: str(obj.pk)}
    )
    assert response.status_code == 302, response.content[:500]
    assert getattr(Mission.objects.get(), f"{kind}_id") == obj.pk


def test_the_edit_page_refuses_alike(team: SimpleNamespace) -> None:
    from apps.travel.tests.helpers import mission_of

    mission = mission_of(team)
    target = reverse(
        "travel:edit_mission", kwargs={"thesis_id": team.thesis.pk, "mission_id": mission.pk}
    )
    client = client_for(team, "student")
    bodies = {
        case: _body(client.post(target, {**FORM, "meeting": raw}))
        for case, raw in _refused_ids(team, "meeting").items()
    }
    assert len(set(bodies.values())) == 1
    mission.refresh_from_db()
    assert mission.meeting_id is None
