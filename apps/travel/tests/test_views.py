"""Views of the presence calendar and the travel settings (/t/<thesis_id>/travel/):
rights per role, POSTs like a browser, the second factor asked after the rights,
masking in the rendered HTML."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from django.urls import reverse
from django.views import View

from apps.accounts.permissions import ThesisPermissionRequiredMixin
from apps.accounts.reauth import RecentAuthRequiredMixin
from apps.travel import views
from apps.travel.models import PresenceDay, TimeSplitTarget, TravelPolicy
from apps.travel.tests.helpers import (
    NOW,
    READERS,
    ROLES,
    D,
    browser_for,
    browser_post,
    client_for,
    make_team,
    url,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _month(team: SimpleNamespace, year: int = 2027, month: int = 1) -> str:
    return url(team, "month", year=year, month=month)


# =========================================================================== rights


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("page", ["calendar", "split", "settings"])
def test_pages_per_role(team: SimpleNamespace, role: str, page: str) -> None:
    response = client_for(team, role).get(url(team, page))
    assert response.status_code == (200 if role in READERS else 403)


@pytest.mark.parametrize("role", ROLES)
def test_month_page_per_role(team: SimpleNamespace, role: str) -> None:
    response = client_for(team, role).get(_month(team))
    assert response.status_code == (200 if role in READERS else 403)


def test_a_member_of_another_thesis_is_refused(team: SimpleNamespace) -> None:
    other = make_team()
    assert client_for(other, "student").get(_month(team)).status_code == 403


@pytest.mark.parametrize(("year", "month"), [(2027, 13), (2026, 8), (2030, 12)])
def test_month_outside_the_thesis_is_a_404(team: SimpleNamespace, year: int, month: int) -> None:
    assert client_for(team, "student").get(_month(team, year, month)).status_code == 404


@pytest.mark.parametrize("view", [views.RulesView, views.TargetView, views.RemindersView])
def test_rights_are_checked_before_the_second_factor(view: Any) -> None:
    mro = view.__mro__
    assert (
        mro.index(ThesisPermissionRequiredMixin)
        < mro.index(RecentAuthRequiredMixin)
        < mro.index(View)
    )


# =========================================================================== presence


def test_the_student_fills_days_like_a_browser(team: SimpleNamespace) -> None:
    client = browser_for(team, "student")
    response = browser_post(
        client,
        _month(team),
        url(team, "presence"),
        {"first": "2027-01-25", "last": "2027-01-31", "kind": "LIRMM"},
    )
    assert response.status_code == 302, response.content[:400]
    assert response["Location"] == _month(team)
    assert PresenceDay.objects.filter(thesis=team.thesis, kind="LIRMM").count() == 5


def test_clearing_days_like_a_browser(team: SimpleNamespace) -> None:
    PresenceDay.objects.create(thesis=team.thesis, day=D(2027, 1, 25), kind="LIRMM")
    client = browser_for(team, "student")
    response = browser_post(
        client,
        _month(team),
        url(team, "presence"),
        {"first": "2027-01-25", "last": "2027-01-25", "kind": ""},
    )
    assert response.status_code == 302
    assert not PresenceDay.objects.exists()


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor", "csi", "guest", "outsider"])
def test_only_the_student_posts_days(team: SimpleNamespace, role: str) -> None:
    response = client_for(team, role).post(
        url(team, "presence"), {"first": "2027-01-25", "last": "2027-01-25", "kind": "LIRMM"}
    )
    assert response.status_code == 403
    assert not PresenceDay.objects.exists()


@pytest.mark.parametrize(
    "data",
    [
        {"first": "2027-01-29", "last": "2027-01-25", "kind": "LIRMM"},
        {"first": "2027-01-25", "last": "2027-01-25", "kind": "HOME"},
        {"first": "25/01/2027", "last": "2027-01-25", "kind": "LIRMM"},
        {"first": "2026-08-31", "last": "2026-08-31", "kind": "LIRMM"},
    ],
)
def test_invalid_days_are_a_400(team: SimpleNamespace, data: dict[str, str]) -> None:
    response = client_for(team, "student").post(url(team, "presence"), data)
    assert response.status_code == 400
    assert not PresenceDay.objects.exists()


def test_only_the_student_sees_the_form(team: SimpleNamespace) -> None:
    assert 'name="kind"' in client_for(team, "student").get(_month(team)).content.decode()
    for role in ("director", "co_supervisor", "tutor"):
        assert 'name="kind"' not in client_for(team, role).get(_month(team)).content.decode()


# =========================================================================== masking


def _fill_week(team: SimpleNamespace) -> None:
    for day, kind in {
        D(2027, 1, 4): "ROUSSET",
        D(2027, 1, 5): "LIRMM",
        D(2027, 1, 6): "REMOTE",
        D(2027, 1, 7): "CONFERENCE",
        D(2027, 1, 8): "LEAVE",
    }.items():
        PresenceDay.objects.create(thesis=team.thesis, day=day, kind=kind)


@pytest.mark.parametrize("page", ["month", "split"])
def test_the_tutor_sees_leave(team: SimpleNamespace, page: str) -> None:
    _fill_week(team)
    target = _month(team) if page == "month" else url(team, "split")
    html = client_for(team, "tutor").get(target).content.decode()
    assert "Congé" in html


@pytest.mark.parametrize("page", ["month", "split"])
def test_the_director_never_reads_the_word_leave(team: SimpleNamespace, page: str) -> None:
    _fill_week(team)
    target = _month(team) if page == "month" else url(team, "split")
    html = client_for(team, "director").get(target).content.decode()
    assert "Indisponible" in html
    for word in ("Congé", "congé", "LEAVE", "Leave", "leave"):
        assert word not in html, word


def test_the_co_supervisor_reads_lirmm_only(team: SimpleNamespace) -> None:
    _fill_week(team)
    for target in (_month(team), url(team, "split")):
        html = client_for(team, "co_supervisor").get(target).content.decode()
        assert "LIRMM" in html
        for word in ("ROUSSET", "Rousset", "REMOTE", "Télétravail", "CONFERENCE", "Conférence"):
            assert word not in html, (target, word)
        for word in ("LEAVE", "Congé", "UNAVAILABLE", "Indisponible"):
            assert word not in html, (target, word)


# =========================================================================== settings


def test_rules_are_the_supervisors_with_a_second_factor(team: SimpleNamespace) -> None:
    data = {
        "remote_counts_as": "LAB",
        "conference_counts_as": "LAB",
        "whit_monday_holiday": "on",
        "gap_alert_points": "12",
        "min_lab_days_per_month": "",
    }
    assert client_for(team, "student").post(url(team, "rules"), data).status_code == 403
    assert client_for(team, "co_supervisor").post(url(team, "rules"), data).status_code == 403
    response = client_for(team, "director").post(url(team, "rules"), data)
    assert response.status_code == 302 and reverse("mfa_reauthenticate") in response["Location"]
    assert not TravelPolicy.objects.exists()
    client = browser_for(team, "director", mfa=True)
    response = browser_post(client, url(team, "settings"), url(team, "rules"), data)
    assert response.status_code == 302, response.content[:400]
    policy = TravelPolicy.objects.get()
    assert (policy.remote_counts_as, policy.gap_alert_points) == ("LAB", 12)
    assert policy.whit_monday_holiday is True
    assert policy.min_lab_days_per_month is None


def test_an_unchecked_box_turns_whit_monday_into_a_working_day(team: SimpleNamespace) -> None:
    data = {
        "remote_counts_as": "ST",
        "conference_counts_as": "LAB",
        "gap_alert_points": "10",
        "min_lab_days_per_month": "3",
    }
    response = client_for(team, "tutor", mfa=True).post(url(team, "rules"), data)
    assert response.status_code == 302
    policy = TravelPolicy.objects.get()
    assert (policy.whit_monday_holiday, policy.min_lab_days_per_month) == (False, 3)


def test_target_is_the_supervisors_with_a_second_factor(team: SimpleNamespace) -> None:
    data = {"cifre_year": "1", "st_percent": "60"}
    assert client_for(team, "student").post(url(team, "target"), data).status_code == 403
    response = client_for(team, "tutor").post(url(team, "target"), data)
    assert reverse("mfa_reauthenticate") in response["Location"]
    client = browser_for(team, "tutor", mfa=True)
    response = browser_post(client, url(team, "settings"), url(team, "target"), data)
    assert response.status_code == 302, response.content[:400]
    assert TimeSplitTarget.objects.get().lab_percent == 40


def test_reminders_are_the_students_with_a_second_factor(team: SimpleNamespace) -> None:
    data = {"order_lead_days": "20", "receipt_grace_days": "5"}
    assert client_for(team, "director").post(url(team, "reminders"), data).status_code == 403
    response = client_for(team, "student").post(url(team, "reminders"), data)
    assert reverse("mfa_reauthenticate") in response["Location"]
    client = browser_for(team, "student", mfa=True)
    response = browser_post(client, url(team, "settings"), url(team, "reminders"), data)
    assert response.status_code == 302, response.content[:400]
    assert TravelPolicy.objects.get().order_lead_days == 20


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("target", {"cifre_year": "9", "st_percent": "60"}),
        ("target", {"cifre_year": "1", "st_percent": "101"}),
        ("reminders", {"order_lead_days": "91", "receipt_grace_days": "5"}),
    ],
)
def test_invalid_settings_are_a_400(team: SimpleNamespace, name: str, data: dict[str, str]) -> None:
    role = "student" if name == "reminders" else "director"
    response = client_for(team, role, mfa=True).post(url(team, name), data)
    assert response.status_code == 400


def test_settings_page_shows_each_form_to_its_owner(team: SimpleNamespace) -> None:
    pages = {r: client_for(team, r).get(url(team, "settings")).content.decode() for r in READERS}
    for role, html in pages.items():
        assert ('name="gap_alert_points"' in html) is (role in ("director", "tutor"))
        assert ('name="st_percent"' in html) is (role in ("director", "tutor"))
        assert ('name="order_lead_days"' in html) is (role == "student")
