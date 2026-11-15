"""The .ics of a mission and of all the missions a user sees (M7, step 7): all-day
events with the destination's LABEL only, a status, a link; never a purpose, an
amount, another place, a conference's name, a lodging. It leaves the platform
(Outlook, ST's Exchange): like M5a's, it carries labels only."""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from apps.conferences.tests.helpers import edition_of, venue_of
from apps.travel import services
from apps.travel.models import Mission
from apps.travel.tests.helpers import NOW, D, client_for, make_team, marker, mission_of, url
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=False)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _one(team: SimpleNamespace, mission: Mission, role: str = "student") -> Any:
    return client_for(team, role).get(url(team, "mission_ics", mission_id=mission.pk))


def _events(body: str) -> list[str]:
    return re.findall(r"BEGIN:VEVENT\r\n(.*?)END:VEVENT", body, re.S)


def test_an_all_day_event_with_labels_only(team: SimpleNamespace) -> None:
    secrets = [marker(), marker(), marker()]
    mission = mission_of(
        team,
        destination="OTHER",
        destination_detail=secrets[0],
        purpose=secrets[1],
        lodging="HOTEL",
        lodging_note=secrets[2],
        payer="OTHER",
    )
    services.add_expense(mission=mission, user=team.student, category="TRANSPORT", estimated="777")
    response = _one(team, mission)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/calendar")
    assert 'filename="mission-2027-02-08.ics"' in response["Content-Disposition"]
    body = response.content.decode()
    for secret in secrets:
        assert secret not in body
    assert "777" not in body and "Hôtel" not in body
    (event,) = _events(body)
    assert "DTSTART;VALUE=DATE:20270208\r\n" in event
    assert "DTEND;VALUE=DATE:20270211\r\n" in event  # exclusive end
    assert "SUMMARY:Déplacement\r\n" in event
    assert "STATUS:TENTATIVE\r\n" in event
    assert f"/travel/missions/{mission.pk}/" in event


@pytest.mark.parametrize(
    ("destination", "summary"),
    [
        ("LIRMM", "Mission — LIRMM Montpellier"),
        ("ROUSSET", "Mission — ST Rousset"),
        ("CONFERENCE", "Conférence"),
    ],
)
def test_summary_is_the_destination_label(
    team: SimpleNamespace, destination: str, summary: str
) -> None:
    extra: dict[str, Any] = {}
    if destination == "CONFERENCE":
        acronym = "ZZ" + marker()[-6:].upper()
        extra["edition"] = edition_of(
            team, venue=venue_of(team, acronym=acronym), location="Lieu fictif"
        )
    mission = mission_of(team, destination=destination, payer="LIRMM", **extra)
    body = _one(team, mission).content.decode()
    assert f"SUMMARY:{summary}\r\n" in body
    if destination == "CONFERENCE":
        assert extra["edition"].venue.acronym not in body and "Lieu fictif" not in body


def test_status_and_sequence_follow_the_mission(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    first = _events(_one(team, mission).content.decode())[0]
    order = services.request_order(mission=mission, user=team.student, estimated_total="1")
    services.decide_order(order=order, user=team.tutor, decision="GRANTED", recent_mfa=True)
    granted = _events(_one(team, mission).content.decode())[0]
    assert "STATUS:CONFIRMED\r\n" in granted
    services.update_mission(
        mission=mission, user=team.student, changes={"end_date": D(2027, 2, 12)}
    )
    services.cancel_mission(mission=mission, user=team.student)
    cancelled = _events(_one(team, mission).content.decode())[0]
    assert "STATUS:CANCELLED\r\n" in cancelled
    sequence = lambda e: int(re.search(r"SEQUENCE:(\d+)", e).group(1))  # type: ignore[union-attr]  # noqa: E731
    assert sequence(first) < sequence(granted) < sequence(cancelled)


def test_lines_are_folded_and_end_with_crlf(team: SimpleNamespace) -> None:
    body = _one(team, mission_of(team)).content
    assert body.endswith(b"\r\n") and b"\n" not in body.replace(b"\r\n", b"")
    assert all(len(line) <= 75 for line in body.split(b"\r\n"))


def test_the_co_supervisor_gets_lirmm_missions_only(team: SimpleNamespace) -> None:
    lirmm = mission_of(team, destination="LIRMM")
    hidden = mission_of(team, destination="ROUSSET")
    assert _one(team, lirmm, "co_supervisor").status_code == 200
    seen = _one(team, hidden, "co_supervisor")
    unknown = client_for(team, "co_supervisor").get(
        url(team, "mission_ics", mission_id=uuid.uuid4())
    )
    assert seen.status_code == unknown.status_code == 403
    assert seen.content == unknown.content
    body = client_for(team, "co_supervisor").get(url(team, "missions_ics")).content.decode()
    assert len(_events(body)) == 1 and str(lirmm.pk) in body and str(hidden.pk) not in body


def test_all_missions_of_the_reader(team: SimpleNamespace) -> None:
    for start in (D(2027, 2, 8), D(2027, 3, 8), D(2027, 4, 5)):
        mission_of(team, start_date=start, end_date=start, destination="ROUSSET")
    response = client_for(team, "tutor").get(url(team, "missions_ics"))
    assert response.status_code == 200
    assert 'filename="missions.ics"' in response["Content-Disposition"]
    assert len(_events(response.content.decode())) == 3


@pytest.mark.parametrize("role", ["csi", "guest", "outsider"])
def test_outside_the_supervision_no_calendar(team: SimpleNamespace, role: str) -> None:
    mission = mission_of(team)
    assert _one(team, mission, role).status_code == 403
    assert client_for(team, role).get(url(team, "missions_ics")).status_code == 403


def test_an_export_is_journaled_with_a_count_only(team: SimpleNamespace) -> None:
    mission_of(team, purpose=marker())
    client_for(team, "director").get(url(team, "missions_ics"))
    (event,) = AuditEvent.objects.filter(action="TRAVEL_ICS_EXPORTED")
    assert event.details_json == {"count": 1}
