"""Views of the meetings (/t/<thesis_id>/meetings/): rights per role, per-meeting
guard before the second factor, POSTs like a browser, Paris times, restricted
content, CSI meetings without minutes, the .ics, the dashboard card and the
linked actions on an objective's page."""

from __future__ import annotations

import datetime as dt
import re
import secrets
from types import SimpleNamespace
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.views import View

from apps.accounts.permissions import ThesisPermissionRequiredMixin
from apps.accounts.reauth import RecentAuthRequiredMixin
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.accounts.tests.helpers import mfa_login
from apps.meetings import services, views
from apps.meetings.models import Meeting, MinutesAcknowledgement
from apps.meetings.tests.helpers import L, marker, meeting_of
from apps.objectives.tests.helpers import D, objective_of
from apps.vault.audit import AuditAction
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = secrets.token_urlsafe(16)
NOW = dt.datetime(2027, 1, 5, 9, 0, tzinfo=dt.UTC)
SITE_ORIGIN = "http://testserver"
RESTRICTED_TEXT = "Contenu restreint"
ROLES = ("student", "director", "co_supervisor", "tutor", "csi", "guest", "outsider")
READERS = ("student", "director", "co_supervisor", "tutor")


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    thesis = ThesisFactory.create(
        cifre_start_date=D(2026, 12, 1),
        first_enrollment_date=D(2026, 12, 15),
        planned_end_date=D(2029, 11, 30),
    )

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(
            thesis=thesis, role=role, user__password=PASSWORD, **kwargs
        ).user

    return SimpleNamespace(
        thesis=thesis,
        student=member(Role.PHD_STUDENT),
        director=member(Role.DIRECTOR),
        co_supervisor=member(Role.CO_SUPERVISOR),
        tutor=member(Role.INDUSTRIAL_TUTOR),
        csi=member(Role.CSI_MEMBER, active_until=timezone.now() + dt.timedelta(days=60)),
        guest=member(Role.GUEST),
        outsider=UserFactory.create(password=PASSWORD),
    )


def _client(team: SimpleNamespace, role: str, *, mfa: bool = False) -> Client:
    client = Client()
    user = getattr(team, role)
    if mfa:
        mfa_login(client, user, PASSWORD)
    else:
        client.force_login(user)  # signed in, but no recent second factor recorded
    return client


def _browser(team: SimpleNamespace, role: str, *, mfa: bool = False) -> Client:
    """A CSRF-enforcing client carrying a real session."""
    signed = _client(team, role, mfa=mfa)
    client = Client(enforce_csrf_checks=True)
    client.cookies = signed.cookies
    return client


def _browser_post(client: Client, page_url: str, url: str, data: dict[str, Any]) -> Any:
    page = client.get(page_url)
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode())
    assert match, "no CSRF token in the page"
    return client.post(
        url, {**data, "csrfmiddlewaretoken": match.group(1)}, HTTP_ORIGIN=SITE_ORIGIN
    )


def _url(team: SimpleNamespace, name: str, **kw: Any) -> str:
    return reverse(f"meetings:{name}", kwargs={"thesis_id": team.thesis.pk, **kw})


def _m(team: SimpleNamespace, meeting: Meeting, name: str) -> str:
    return _url(team, name, meeting_id=meeting.pk)


def _form(team: SimpleNamespace, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": "SUPERVISION",
        "presentation_venue": "",
        "title": "Point fictif",
        "date": "2027-07-12",
        "time": "14:00",
        "duration_minutes": "60",
        "location": "VISIO",
        "location_detail": "",
        "visio_link": "",
        "level": "PUBLIC",
        "attendees": [str(team.student.pk), str(team.director.pk)],
    }
    data.update(overrides)
    return data


def _title_tag(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, re.S)
    return match.group(1) if match else ""


# =========================================================================== rights


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("page", ["list", "register", "new"])
def test_pages_per_role(team: SimpleNamespace, role: str, page: str) -> None:
    response = _client(team, role).get(_url(team, page))
    assert response.status_code == (200 if role in READERS else 403)


@pytest.mark.parametrize("role", ROLES)
def test_detail_per_role(team: SimpleNamespace, role: str) -> None:
    meeting = meeting_of(team)
    response = _client(team, role).get(_m(team, meeting, "detail"))
    assert response.status_code == (200 if role in READERS else 403)


@pytest.mark.parametrize("role", ROLES)
def test_create_per_role(team: SimpleNamespace, role: str) -> None:
    response = _client(team, role).post(_url(team, "new"), _form(team))
    if role in READERS:
        assert response.status_code == 302
        assert Meeting.objects.count() == 1
    else:
        assert response.status_code == 403
        assert not Meeting.objects.exists()


def test_a_meeting_of_another_thesis_through_my_url_is_refused(team: SimpleNamespace) -> None:
    other = meeting_of(_other_team())
    response = _client(team, "student").get(_m(team, other, "detail"))
    assert response.status_code == 403


def _other_team() -> SimpleNamespace:
    from apps.meetings.tests.helpers import make_team

    return make_team()


# =========================================================================== create, Paris time


def test_create_posted_like_a_browser_in_paris_time(team: SimpleNamespace) -> None:
    client = _browser(team, "student")
    response = _browser_post(client, _url(team, "new"), _url(team, "new"), _form(team))
    assert response.status_code == 302, response.content[:500]
    meeting = Meeting.objects.get()
    assert meeting.starts_at == dt.datetime(2027, 7, 12, 12, 0, tzinfo=dt.UTC)  # summer time
    assert response["Location"] == _m(team, meeting, "detail")


@pytest.mark.parametrize(
    ("day", "code"), [("2027-03-28", "existe pas"), ("2027-10-31", "deux fois")]
)
def test_a_nonexistent_or_ambiguous_time_is_refused(
    team: SimpleNamespace, day: str, code: str
) -> None:
    response = _client(team, "student").post(_url(team, "new"), _form(team, date=day, time="02:30"))
    assert response.status_code == 400
    assert code in response.content.decode()
    assert not Meeting.objects.exists()


def test_an_attendee_who_cannot_read_the_level_is_refused(team: SimpleNamespace) -> None:
    data = _form(team, level="INTERNAL_ST", attendees=[str(team.student.pk), str(team.director.pk)])
    response = _client(team, "student").post(_url(team, "new"), data)
    assert response.status_code == 400
    assert not Meeting.objects.exists()


def test_the_title_stays_in_the_body(team: SimpleNamespace) -> None:
    title = marker()
    meeting = meeting_of(team, title=title)
    response = _client(team, "director").get(_m(team, meeting, "detail"))
    html = response.content.decode()
    assert title in html
    assert title not in _title_tag(html)
    assert title not in _m(team, meeting, "detail")


def test_restricted_content_for_a_reader_without_the_level(team: SimpleNamespace) -> None:
    title = marker()
    meeting = meeting_of(
        team, level=L.INTERNAL_ST, title=title, attendees=[team.student, team.tutor]
    )
    html = _client(team, "co_supervisor").get(_m(team, meeting, "detail")).content.decode()
    assert title not in html and RESTRICTED_TEXT in html
    listing = _client(team, "co_supervisor").get(_url(team, "list")).content.decode()
    assert title not in listing and RESTRICTED_TEXT in listing


def test_one_audited_read_per_page_of_a_confidential_meeting(team: SimpleNamespace) -> None:
    meeting = meeting_of(
        team, level=L.CONFIDENTIAL, attendees=[team.student, team.director, team.tutor]
    )
    services.add_decision(meeting=meeting, user=team.student, text="Décision fictive")
    before = AuditEvent.objects.filter(action=AuditAction.SEALED_TEXT_READ).count()
    assert _client(team, "director").get(_m(team, meeting, "detail")).status_code == 200
    assert AuditEvent.objects.filter(action=AuditAction.SEALED_TEXT_READ).count() == before + 1


# =========================================================================== writing


def test_a_non_writer_cannot_post(team: SimpleNamespace) -> None:
    meeting = meeting_of(team, attendees=[team.student, team.director])
    response = _client(team, "tutor").post(_m(team, meeting, "agenda"), {"agenda": "Ajout"})
    assert response.status_code == 403
    response = _client(team, "director").post(_m(team, meeting, "agenda"), {"agenda": "Ajout"})
    assert response.status_code == 302


def test_minutes_posted_like_a_browser_then_stale(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    client = _browser(team, "director")
    response = _browser_post(
        client,
        _m(team, meeting, "detail"),
        _m(team, meeting, "minutes"),
        {"minutes": "CR fictif", "version": "0"},
    )
    assert response.status_code == 302, response.content[:500]
    stale = _client(team, "student").post(
        _m(team, meeting, "minutes"), {"minutes": "Autre", "version": "0"}
    )
    assert stale.status_code == 409
    assert "Autre" in stale.content.decode()  # what was typed is kept


def test_a_csi_meeting_has_no_minutes_yet(team: SimpleNamespace) -> None:
    meeting = meeting_of(team, kind=Meeting.Kind.CSI)
    html = _client(team, "student").get(_m(team, meeting, "detail")).content.decode()
    assert "disponible prochainement" in html
    assert 'name="minutes"' not in html
    response = _client(team, "student").post(
        _m(team, meeting, "minutes"), {"minutes": "CR fictif", "version": "0"}
    )
    assert response.status_code == 409
    assert Meeting.objects.get(pk=meeting.pk).minutes_version == 0


def test_prefill_view(team: SimpleNamespace) -> None:
    source = meeting_of(team)
    services.add_action(
        meeting=source,
        user=team.student,
        title="Action ouverte fictive",
        assignee=team.student,
        due_date=D(2027, 3, 1),
    )
    target = meeting_of(team, starts_at=NOW + dt.timedelta(days=10))
    response = _client(team, "student").post(_m(team, target, "prefill"))
    assert response.status_code == 302
    html = _client(team, "student").get(_m(team, target, "detail")).content.decode()
    assert "Action ouverte fictive" in html
    nothing = _client(team, "student").post(_m(team, source, "prefill"))
    assert nothing.status_code == 409


def test_duplicate_view(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    client = _browser(team, "student")
    response = _browser_post(
        client,
        _m(team, meeting, "duplicate"),
        _m(team, meeting, "duplicate"),
        {"date": "2027-01-19", "time": "14:00"},
    )
    assert response.status_code == 302, response.content[:500]
    copy = Meeting.objects.exclude(pk=meeting.pk).get()
    assert copy.starts_at == dt.datetime(2027, 1, 19, 13, 0, tzinfo=dt.UTC)


# =========================================================================== second factor


@pytest.mark.parametrize("view", [views.ApproveView, views.LevelView])
def test_the_meeting_guard_comes_before_the_second_factor(view: type) -> None:
    mro = view.__mro__
    guard = mro.index(ThesisPermissionRequiredMixin)
    per_meeting = mro.index(views._MeetingGuard)
    reauth = mro.index(RecentAuthRequiredMixin)
    assert guard < per_meeting < reauth < mro.index(View)


def test_approve(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    services.save_minutes(meeting=meeting, user=team.student, minutes="CR fictif", version=0)
    url = _m(team, meeting, "approve")
    # Not an attendee: 403, never asked for a second factor.
    assert _client(team, "tutor").post(url, {"version": "1"}).status_code == 403
    # An attendee without a recent second factor is sent to re-authenticate.
    response = _client(team, "director").post(url, {"version": "1"})
    assert response.status_code == 302
    assert reverse("mfa_reauthenticate") in response["Location"]
    assert not MinutesAcknowledgement.objects.exists()
    client = _browser(team, "director", mfa=True)
    response = _browser_post(client, _m(team, meeting, "detail"), url, {"version": "1"})
    assert response.status_code == 302
    assert MinutesAcknowledgement.objects.get().kind == "APPROVED"


def test_read_needs_no_second_factor(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    services.save_minutes(meeting=meeting, user=team.student, minutes="CR fictif", version=0)
    response = _client(team, "director").post(_m(team, meeting, "read"), {"version": "1"})
    assert response.status_code == 302
    assert MinutesAcknowledgement.objects.get().kind == "READ"
    again = _client(team, "director").post(_m(team, meeting, "read"), {"version": "1"})
    assert again.status_code == 409


def test_change_level(team: SimpleNamespace) -> None:
    meeting = meeting_of(team, attendees=[team.student, team.director])
    url = _m(team, meeting, "level")
    assert _client(team, "tutor").post(url, {"level": "CONFIDENTIAL"}).status_code == 403
    response = _client(team, "student").post(url, {"level": "CONFIDENTIAL"})
    assert response.status_code == 302
    assert reverse("mfa_reauthenticate") in response["Location"]
    assert Meeting.objects.get(pk=meeting.pk).level == "PUBLIC"
    response = _client(team, "student", mfa=True).post(url, {"level": "CONFIDENTIAL"})
    assert response.status_code == 302
    assert Meeting.objects.get(pk=meeting.pk).level == "CONFIDENTIAL"


# =========================================================================== .ics


def test_ics_carries_labels_never_texts(team: SimpleNamespace) -> None:
    icalendar = pytest.importorskip("icalendar")
    title, link, room = marker(), "https://visio.example.org/" + marker(), marker()
    meeting = meeting_of(
        team, title=title, visio_link=link, location_detail=room, location="ROUSSET"
    )
    services.save_agenda(meeting=meeting, user=team.student, agenda=marker())
    response = _client(team, "director").get(_m(team, meeting, "ics"))
    assert response.status_code == 200
    assert response["Content-Type"] == "text/calendar; charset=utf-8"
    assert response["Content-Disposition"].startswith("attachment;")
    assert response["Cache-Control"] == "no-store"
    body = response.content.decode()
    for secret in (title, link, room, team.director.email, team.student.email):
        assert secret not in body
    event = icalendar.Calendar.from_ical(response.content).walk("VEVENT")[0]
    assert str(event["SUMMARY"]) == "Point avec le directeur ou un co-encadrant"
    assert str(event["LOCATION"]) == "ST Rousset"
    assert event["DTSTART"].dt == meeting.starts_at
    assert event["SEQUENCE"] == 0
    assert str(event["STATUS"]) == "CONFIRMED"
    assert "ATTENDEE" not in event
    assert AuditEvent.objects.filter(action=AuditAction.MEETING_ICS_EXPORTED).count() == 1


def test_ics_of_a_cancelled_meeting(team: SimpleNamespace) -> None:
    icalendar = pytest.importorskip("icalendar")
    meeting = meeting_of(team)
    services.cancel_meeting(meeting=meeting, user=team.student)
    response = _client(team, "student").get(_m(team, meeting, "ics"))
    event = icalendar.Calendar.from_ical(response.content).walk("VEVENT")[0]
    assert (str(event["STATUS"]), event["SEQUENCE"]) == ("CANCELLED", 1)


@pytest.mark.parametrize("role", ["csi", "guest", "outsider"])
def test_ics_is_refused_outside_the_team(team: SimpleNamespace, role: str) -> None:
    meeting = meeting_of(team)
    assert _client(team, role).get(_m(team, meeting, "ics")).status_code == 403


# =========================================================================== dashboard, objective


def test_dashboard_card(team: SimpleNamespace, time_machine: Any) -> None:
    title = marker()
    meeting = meeting_of(team, title=title, starts_at=NOW + dt.timedelta(days=3))
    services.add_action(
        meeting=meeting,
        user=team.student,
        title="Action en retard fictive",
        assignee=team.director,
        due_date=D(2027, 1, 6),
    )
    time_machine.move_to(dt.datetime(2027, 1, 7, 9, 0, tzinfo=dt.UTC), tick=True)
    html = (
        _client(team, "director")
        .get(reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk}))
        .content.decode()
    )
    assert "Disponible dans un prochain module" not in html
    assert title in html
    assert "1 action en retard" in html
    assert "Action en retard fictive" in html


def test_linked_actions_on_the_objective_page(team: SimpleNamespace) -> None:
    objective = objective_of(team)
    meeting = meeting_of(team)
    services.add_action(
        meeting=meeting,
        user=team.student,
        title="Action liée fictive",
        assignee=team.student,
        due_date=D(2027, 3, 1),
        objective=objective,
    )
    page = reverse(
        "objectives:detail", kwargs={"thesis_id": team.thesis.pk, "objective_id": objective.pk}
    )
    fragment = _url(team, "objective_actions", objective_id=objective.pk)
    assert fragment in _client(team, "director").get(page).content.decode()
    assert "Action liée fictive" in _client(team, "director").get(fragment).content.decode()
    csi_page = _client(team, "csi").get(page)
    assert csi_page.status_code == 200
    assert fragment not in csi_page.content.decode()
    assert _client(team, "csi").get(fragment).status_code == 403


# =========================================================================== end-of-module review


def test_the_duplicate_page_is_refused_to_a_non_writer(team: SimpleNamespace) -> None:
    meeting = meeting_of(team, attendees=[team.student, team.director])
    assert _client(team, "tutor").get(_m(team, meeting, "duplicate")).status_code == 403
    assert _client(team, "director").get(_m(team, meeting, "duplicate")).status_code == 200


def test_ics_download_headers_match_the_vault_downloads(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    response = _client(team, "student").get(_m(team, meeting, "ics"))
    assert response["Content-Security-Policy"] == "sandbox; default-src 'none'"
    assert response["Referrer-Policy"] == "no-referrer"
    assert response["X-Content-Type-Options"] == "nosniff"


def test_a_sub_object_not_of_the_meeting_is_an_audited_refusal(team: SimpleNamespace) -> None:
    mine = meeting_of(team)
    other = meeting_of(team, starts_at=NOW + dt.timedelta(days=10))
    decision = services.add_decision(meeting=other, user=team.student, text="Décision fictive")
    url = _url(team, "withdraw_decision", meeting_id=mine.pk, decision_id=decision.pk)
    assert _client(team, "student").post(url).status_code == 403
    denied = AuditEvent.objects.filter(action=AuditAction.MEETING_ACCESS_DENIED).last()
    assert denied is not None
    assert denied.details_json["reason"] == "unknown_decision"
