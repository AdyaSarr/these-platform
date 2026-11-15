"""The platform follows the supervisor's MISSION APPROVAL; the official mission order is
issued outside it (ST's HR tool, the LIRMM's manager). Labels say "accord de mission";
the checklist has "official mission order obtained" and its document, stored in the
vault at the mission's level and readable only by those who see the mission; marking a
mission done without it warns (never blocks)."""

from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.permissions import can_access_document
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.travel import services
from apps.travel.models import Mission
from apps.travel.tests.helpers import (
    NOW,
    browser_for,
    browser_post,
    client_for,
    make_team,
    mission_of,
    url,
)
from apps.vault.models import AuditEvent, Document

pytestmark = pytest.mark.django_db(transaction=True)

HELP = "Cet accord ne remplace pas l'ordre de mission officiel de ST ou du LIRMM."


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _pdf(name: str = "ordre.pdf") -> SimpleUploadedFile:
    return SimpleUploadedFile(
        name, b"%PDF-1.7\n%ordre de mission fictif\n%%EOF\n", "application/pdf"
    )


def _attach(team: SimpleNamespace, mission: Mission) -> Document:
    services.attach_official_order(mission=mission, user=team.student, upload=_pdf())
    mission.refresh_from_db()
    assert mission.official_order_document_id is not None
    return Document.objects.get(pk=mission.official_order_document_id)


def _m(team: SimpleNamespace, mission: Mission, name: str = "mission") -> str:
    return url(team, name, mission_id=mission.pk)


def _html(response: Any) -> str:
    from html import unescape

    return unescape(response.content.decode())


# =========================================================================== labels


def test_the_platform_speaks_of_a_mission_approval(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    services.request_order(mission=mission, user=team.student, estimated_total="100")
    pages = {
        "mission": _html(client_for(team, "student").get(_m(team, mission))),
        "list": _html(client_for(team, "student").get(url(team, "missions"))),
        "queue": _html(client_for(team, "tutor").get(url(team, "orders"))),
    }
    assert "Accord de mission demandé" in pages["mission"]
    assert "Accord de mission demandé" in pages["list"]
    assert "Accords de mission à décider" in pages["queue"]
    for name in ("mission", "queue"):
        assert HELP in pages[name], name
    for name, html in pages.items():
        # "ordre de mission" only ever names the OFFICIAL order.
        for match in re.finditer(r"[Oo]rdres? de mission", html):
            assert html[match.end() : match.end() + 9] == " officiel", (
                name,
                html[match.start() - 40 : match.end() + 20],
            )


# =========================================================================== checklist


def test_the_checklist_ticks_the_official_order(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    changed = services.update_mission(
        mission=mission, user=team.student, changes={"official_order_obtained": True}
    )
    assert changed == ["official_order_obtained"]
    mission.refresh_from_db()
    assert mission.official_order_obtained is True


def test_the_official_document_is_stored_at_the_missions_level(team: SimpleNamespace) -> None:
    mission = mission_of(team, payer="ST")
    document = _attach(team, mission)
    mission.refresh_from_db()
    assert document.confidentiality == mission.level == "INTERNAL_ST"
    assert mission.official_order_obtained is True
    assert document.thesis_id == team.thesis.pk
    events = AuditEvent.objects.filter(action="MISSION_OFFICIAL_ORDER_ATTACHED")
    assert [e.details_json for e in events] == [{"document_id": str(document.pk), "version": 1}]


def test_a_second_upload_is_a_new_version(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    first = _attach(team, mission)
    second = _attach(team, mission)
    assert first.pk == second.pk
    second.refresh_from_db()
    assert second.current_version == 2


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor"])
def test_only_the_student_attaches(team: SimpleNamespace, role: str) -> None:
    from django.core.exceptions import PermissionDenied

    mission = mission_of(team)
    with pytest.raises(PermissionDenied):
        services.attach_official_order(mission=mission, user=getattr(team, role), upload=_pdf())
    assert not Document.objects.exists()


def test_attach_like_a_browser(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    client = browser_for(team, "student")
    response = browser_post(
        client, _m(team, mission), _m(team, mission, "official_order"), {"file": _pdf()}
    )
    assert response.status_code == 302, response.content[:500]
    mission.refresh_from_db()
    assert mission.official_order_document_id is not None


# =========================================================================== reading


def test_only_those_who_see_the_mission_and_its_level_read_it(team: SimpleNamespace) -> None:
    st = _attach(team, mission_of(team, payer="ST"))  # INTERNAL_ST
    lab = _attach(team, mission_of(team, payer="LIRMM"))  # INTERNAL_LIRMM
    hidden = _attach(team, mission_of(team, destination="ROUSSET", payer="ST", level=L.PUBLIC))
    readers = {
        role: {
            name
            for name, doc in (("st", st), ("lab", lab), ("hidden", hidden))
            if can_access_document(getattr(team, role), doc)
        }
        for role in ("student", "director", "co_supervisor", "tutor")
    }
    assert readers == {
        "student": {"st", "lab", "hidden"},
        "director": {"lab", "hidden"},  # INTERNAL_ST is not his
        "co_supervisor": {"lab"},  # a PUBLIC document of a mission he does not see: no
        "tutor": {"st", "hidden"},
    }


def test_the_mission_page_links_the_document_for_its_readers(team: SimpleNamespace) -> None:
    mission = mission_of(team, payer="ST")
    document = _attach(team, mission)
    link = f"/documents/{document.pk}/"
    assert link in _html(client_for(team, "tutor").get(_m(team, mission)))
    assert link not in _html(client_for(team, "director").get(_m(team, mission)))


def test_a_level_change_moves_the_document(team: SimpleNamespace) -> None:
    mission = mission_of(team, payer="ST")
    document = _attach(team, mission)
    services.change_mission_level(
        mission=mission, user=team.student, level=L.CONFIDENTIAL, recent_mfa=True
    )
    document.refresh_from_db()
    assert document.confidentiality == "CONFIDENTIAL"
    assert can_access_document(team.director, document)


# =========================================================================== done


def _granted_and_back(team: SimpleNamespace, time_machine: Any, **kw: Any) -> Mission:
    mission: Mission = mission_of(team, **kw)
    if mission.payer != "OTHER":
        order = services.request_order(mission=mission, user=team.student, estimated_total="10")
        decider = team.tutor if mission.payer == "ST" else team.director
        services.decide_order(order=order, user=decider, decision="GRANTED", recent_mfa=True)
    time_machine.move_to(dt.datetime(2027, 2, 11, 9, 0, tzinfo=dt.UTC), tick=True)
    return mission


WARNING = "l'ordre de mission officiel n'est pas coché"


@pytest.mark.parametrize("payer", ["ST", "LIRMM"])
def test_done_without_the_official_order_warns(
    team: SimpleNamespace, time_machine: Any, payer: str
) -> None:
    mission = _granted_and_back(team, time_machine, payer=payer)
    response = client_for(team, "student").post(_m(team, mission, "mission_done"), follow=True)
    assert response.status_code == 200
    mission.refresh_from_db()
    assert mission.status == "DONE"  # warned, not blocked
    assert WARNING in _html(response)


def test_done_with_the_official_order_does_not_warn(
    team: SimpleNamespace, time_machine: Any
) -> None:
    mission = _granted_and_back(team, time_machine, payer="ST")
    services.update_mission(
        mission=mission, user=team.student, changes={"official_order_obtained": True}
    )
    response = client_for(team, "student").post(_m(team, mission, "mission_done"), follow=True)
    assert WARNING not in _html(response)


def test_another_payer_is_never_warned(team: SimpleNamespace, time_machine: Any) -> None:
    mission = _granted_and_back(team, time_machine, payer="OTHER")
    response = client_for(team, "student").post(_m(team, mission, "mission_done"), follow=True)
    assert Mission.objects.get(pk=mission.pk).status == "DONE"
    assert WARNING not in _html(response)
