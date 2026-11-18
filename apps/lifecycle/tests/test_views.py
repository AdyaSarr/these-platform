"""Views of the lifecycle (M13a, criterion 9): one allowed and one refused test per
role, POST with CSRF and HTTP_ORIGIN, a recent second factor (except withdrawal)."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.models import Thesis
from apps.lifecycle.models import ClosureRequest
from apps.lifecycle.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)
S = Thesis.Status
READERS = ("student", "director", "co_supervisor", "tutor", "st_co_supervisor", "csi")


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _post(team: SimpleNamespace, user: Any, name: str, data: dict[str, Any], **kw: Any) -> Any:
    client = h.mfa_browser(user)
    return h.browser_post(client, h.url(team, "page"), h.url(team, name, **kw), data)


# --------------------------------------------------------------------------- the page


@pytest.mark.parametrize("attr", READERS)
def test_every_member_who_sees_the_thesis_sees_the_page(team: SimpleNamespace, attr: str) -> None:
    response = h.client_for(getattr(team, attr)).get(h.url(team, "page"))
    assert response.status_code == 200
    assert "Fin de thèse" in response.content.decode()


@pytest.mark.parametrize("attr", ["guest", "outsider"])
def test_the_guest_and_an_outsider_do_not(team: SimpleNamespace, attr: str) -> None:
    assert h.client_for(getattr(team, attr)).get(h.url(team, "page")).status_code == 403


def test_the_forms_follow_the_rights(team: SimpleNamespace) -> None:
    director = h.client_for(team.director).get(h.url(team, "page")).content.decode()
    student = h.client_for(team.student).get(h.url(team, "page")).content.decode()
    tutor = h.client_for(team.tutor).get(h.url(team, "page")).content.decode()
    assert h.url(team, "defend") in director and h.url(team, "defend") not in student
    assert h.url(team, "request") in director and h.url(team, "request") in student
    assert h.url(team, "request") not in tutor and h.url(team, "defend") not in tutor


def test_the_team_page_links_to_it(team: SimpleNamespace) -> None:
    from django.urls import reverse

    page = h.client_for(team.director).get(
        reverse("accounts:team", kwargs={"thesis_id": team.thesis.pk})
    )
    assert h.url(team, "page") in page.content.decode()


# --------------------------------------------------------------------------- the passages


def test_the_director_declares_the_defence(team: SimpleNamespace) -> None:
    response = _post(team, team.director, "defend", {"defended_on": "2027-06-01"})
    assert response.status_code == 302 and h.status_of(team) == S.DEFENDED


@pytest.mark.parametrize("attr", ["student", "co_supervisor", "tutor", "csi"])
def test_no_one_else_declares_it(team: SimpleNamespace, attr: str) -> None:
    client = h.mfa_browser(getattr(team, attr))
    response = h.browser_post(
        client, h.url(team, "page"), h.url(team, "defend"), {"defended_on": "2027-06-01"}
    )
    assert response.status_code == 403 and h.status_of(team) == S.ACTIVE


def test_a_declaration_without_a_recent_second_factor_asks_for_it(team: SimpleNamespace) -> None:
    client = h.client_for(team.director)  # signed in, no recent second factor
    response = client.post(h.url(team, "defend"), {"defended_on": "2027-06-01"})
    assert response.status_code == 302 and "reauthenticate" in response["Location"]
    assert h.status_of(team) == S.ACTIVE


def test_the_director_cancels_it(team: SimpleNamespace) -> None:
    h.defended(team)
    response = _post(team, team.director, "cancel", {"reason": "wrong_date"})
    assert response.status_code == 302 and h.status_of(team) == S.ACTIVE


def test_the_student_does_not_cancel_it(team: SimpleNamespace) -> None:
    h.defended(team)
    response = _post(team, team.student, "cancel", {"reason": "wrong_date"})
    assert response.status_code == 403 and h.status_of(team) == S.DEFENDED


@pytest.mark.parametrize("attr", ["student", "director"])
def test_the_student_or_the_director_requests_the_closure(team: SimpleNamespace, attr: str) -> None:
    response = _post(team, getattr(team, attr), "request", {"reason": "student_withdrew"})
    assert response.status_code == 302 and ClosureRequest.objects.get().is_open


@pytest.mark.parametrize("attr", ["co_supervisor", "tutor", "st_co_supervisor", "csi"])
def test_no_one_else_requests_it(team: SimpleNamespace, attr: str) -> None:
    response = _post(team, getattr(team, attr), "request", {"reason": "student_withdrew"})
    assert response.status_code == 403 and not ClosureRequest.objects.exists()


def test_the_other_party_confirms(team: SimpleNamespace) -> None:
    request = h.requested(team, reason="other")
    response = _post(team, team.student, "confirm", {}, request_id=request.pk)
    assert response.status_code == 302 and h.status_of(team) == S.CLOSED


def test_the_requester_does_not_confirm(team: SimpleNamespace) -> None:
    request = h.requested(team, reason="other")
    response = _post(team, team.director, "confirm", {}, request_id=request.pk)
    assert response.status_code in (400, 403) and h.status_of(team) == S.ACTIVE


@pytest.mark.parametrize("attr", ["co_supervisor", "tutor", "csi"])
def test_no_one_else_confirms(team: SimpleNamespace, attr: str) -> None:
    request = h.requested(team, reason="other")
    response = _post(team, getattr(team, attr), "confirm", {}, request_id=request.pk)
    assert response.status_code == 403 and h.status_of(team) == S.ACTIVE


def test_a_withdrawal_needs_no_second_factor(team: SimpleNamespace) -> None:
    from django.test import Client

    request = h.requested(team, reason="other")
    client = Client(enforce_csrf_checks=True)
    client.force_login(team.student)  # no recent second factor
    response = h.browser_post(
        client, h.url(team, "page"), h.url(team, "withdraw", request_id=request.pk), {}
    )
    assert response.status_code == 302
    assert not ClosureRequest.objects.get(pk=request.pk).is_open


@pytest.mark.parametrize("attr", ["co_supervisor", "tutor", "csi"])
def test_no_one_else_withdraws(team: SimpleNamespace, attr: str) -> None:
    request = h.requested(team, reason="other")
    client = h.client_for(getattr(team, attr))
    response = client.post(h.url(team, "withdraw", request_id=request.pk))
    assert response.status_code == 403
    assert ClosureRequest.objects.get(pk=request.pk).is_open


def test_a_closed_thesis_offers_no_form(team: SimpleNamespace, time_machine: Any) -> None:
    h.closed(team, time_machine)
    page = h.client_for(team.student).get(h.url(team, "page")).content.decode()
    assert "<form" not in page.split("<main", 1)[1]
    response = h.client_for(team.director).post(
        h.url(team, "defend"), {"defended_on": "2027-06-01"}
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- review of the page


def test_a_wrong_date_is_answered_with_the_rule(team: SimpleNamespace) -> None:
    response = _post(team, team.director, "defend", {"defended_on": "2027-06-11"})
    assert response.status_code == 400
    assert (
        "La date de soutenance doit être passée et postérieure à la première inscription "
        "(le 15 décembre 2026)." in response.content.decode()
    )


def _details(page: str, url: str) -> str:
    """The <details> block that holds the form posting to ``url``."""
    for block in re.findall(r"<details\b.*?</details>", page, re.S):
        if f'action="{url}"' in block:
            return str(block)
    raise AssertionError(f"no <details> for {url}")


def test_closing_without_a_defence_is_the_discreet_way(team: SimpleNamespace) -> None:
    page = h.client_for(team.director).get(h.url(team, "page")).content.decode()
    declare = _details(page, h.url(team, "defend"))
    abandon = _details(page, h.url(team, "request"))
    assert re.search(r'<summary class="btn btn-primary">', declare)
    assert re.search(r'<summary class="btn btn-ghost">', abandon)
    assert "Clore sans soutenance (abandon)" in abandon
    assert "Demander la clôture" not in page
    assert re.search(r'<select [^>]*name="reason" required>\s*<option value="">', abandon)
    assert re.search(r"<p>[^<]{40,}</p>", abandon)  # an explanation


def test_the_reason_of_an_abandonment_is_required(team: SimpleNamespace) -> None:
    response = _post(team, team.student, "request", {"reason": ""})
    assert response.status_code == 400 and not ClosureRequest.objects.exists()


def test_after_the_defence_it_is_a_closure_request(
    team: SimpleNamespace, time_machine: Any
) -> None:
    h.defended(team)
    time_machine.move_to(h.CLOSABLE, tick=True)
    page = h.client_for(team.director).get(h.url(team, "page")).content.decode()
    assert "Demander la clôture" in _details(page, h.url(team, "request"))
    assert "abandon" not in page


@pytest.mark.parametrize(
    ("status", "attr", "sentence"),
    [
        ("SETUP", "director", "La soutenance pourra être déclarée une fois la thèse active, "
                              "c'est-à-dire le dossier d'inscription validé."),
        ("SETUP", "co_supervisor", "La soutenance pourra être déclarée une fois la thèse "
                                   "active, c'est-à-dire le dossier d'inscription validé."),
        ("ACTIVE", "co_supervisor", "Seuls le directeur et le doctorant font avancer la fin "
                                    "de thèse."),
        ("ACTIVE", "csi", "Seuls le directeur et le doctorant font avancer la fin de thèse."),
        ("DEFENDED", "tutor", "Seuls le directeur et le doctorant font avancer la fin de thèse."),
        ("DEFENDED", "student", "La clôture pourra être demandée à partir du 1 septembre 2027."),
        ("CLOSED", "director", "La thèse est close\u00a0: il n'y a plus d'étape."),
    ],
)  # fmt: skip
def test_when_nothing_can_be_done_a_sentence_says_why(
    team: SimpleNamespace, time_machine: Any, status: str, attr: str, sentence: str
) -> None:
    if status == "SETUP":
        Thesis.objects.filter(pk=team.thesis.pk).update(status=S.SETUP)
    elif status == "DEFENDED":
        h.defended(team)
    elif status == "CLOSED":
        h.closed(team, time_machine)
    page = h.client_for(getattr(team, attr)).get(h.url(team, "page")).content.decode()
    assert sentence in page
    assert "<form" not in page.split("<main", 1)[1].split("</main>", 1)[0]
