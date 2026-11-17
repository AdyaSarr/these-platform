"""Views of the activity app: for each view, one allowed and one refused test per
role (the seven roles); the GET of an element only marks it read and redirects
(requirement b); « mark all read » and preferences are POST with CSRF.

Refusals look alike: an unknown event, one of another thesis and one the reader
does not (or no longer) read give the same answer.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.roles import Role
from apps.activity.models import EventRead, NotificationPreference
from apps.activity.tests import helpers as h
from apps.core.confidentiality import ConfidentialityLevel as L

pytestmark = pytest.mark.django_db(transaction=True)

READERS = [r for r in h.ROLE_ATTRS if r is not Role.GUEST]


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return h.make_team()


def _user(team: SimpleNamespace, role: Role) -> Any:
    return getattr(team, h.ROLE_ATTRS[role])


def _public_event(team: SimpleNamespace) -> Any:
    """An event every member but the student and the guest reads."""
    h.objective(team, L.PUBLIC)
    return h.events_of("OBJECTIVE_CREATED")[-1]


# --------------------------------------------------------------------------- feed


@pytest.mark.parametrize("role", READERS, ids=lambda r: r.value)
def test_the_feed_is_shown_to_every_member(team: SimpleNamespace, role: Role) -> None:
    response = h.client_for(_user(team, role)).get(h.url(team, "feed"))
    assert response.status_code == 200


@pytest.mark.parametrize("role", list(h.ROLE_ATTRS), ids=lambda r: r.value)
def test_the_feed_of_another_thesis_is_refused(team: SimpleNamespace, role: Role) -> None:
    other = h.make_team()
    response = h.client_for(_user(team, role)).get(h.url(other, "feed"))
    assert response.status_code == 403


def test_the_guest_and_an_outsider_are_refused(team: SimpleNamespace) -> None:
    for user in (team.guest, team.outsider):
        assert h.client_for(user).get(h.url(team, "feed")).status_code == 403


def test_the_feed_names_each_element_and_links_it(team: SimpleNamespace) -> None:
    doc = h.document(team, L.PUBLIC, title="Rapport fictif de janvier")
    event = h.events_of("DOCUMENT_ADDED")[-1]
    page = h.client_for(team.director).get(h.url(team, "feed")).content.decode()
    assert "Rapport fictif de janvier" in page
    assert h.url(team, "open", event_id=event.pk) in page
    assert str(doc.pk) not in page  # the link goes through the event, marking it read


# --------------------------------------------------------------------------- open


@pytest.mark.parametrize("role", [r for r in READERS if r is not Role.PHD_STUDENT],
                         ids=lambda r: r.value)  # fmt: skip
def test_opening_marks_read_and_redirects(team: SimpleNamespace, role: Role) -> None:
    event = _public_event(team)
    user = _user(team, role)
    response = h.client_for(user).get(h.url(team, "open", event_id=event.pk))
    assert response.status_code == 302
    assert response["Location"].startswith(f"/t/{team.thesis.pk}/objectives/")
    assert EventRead.objects.filter(event=event, user=user).count() == 1


@pytest.mark.parametrize("role", list(h.ROLE_ATTRS), ids=lambda r: r.value)
def test_an_unreadable_event_looks_unknown(team: SimpleNamespace, role: Role) -> None:
    """Unknown id, another thesis's event, an event the reader does not read:
    the same answer, nothing marked."""
    other = h.make_team()
    h.objective(other, L.PUBLIC)
    foreign = h.events_of("OBJECTIVE_CREATED")[-1]
    h.document(team, L.CONFIDENTIAL)  # nobody but the student, the director, the tutor
    hidden = h.events_of("DOCUMENT_ADDED")[-1]
    client = h.client_for(_user(team, role))
    answers = []
    for event_id in (uuid.uuid4(), foreign.pk):
        response = client.get(h.url(team, "open", event_id=event_id))
        answers.append((response.status_code, response.content))
    if role in (Role.CO_SUPERVISOR, Role.INDUSTRIAL_CO_SUPERVISOR, Role.CSI_MEMBER,
                Role.GUEST, Role.PHD_STUDENT):  # fmt: skip
        response = client.get(h.url(team, "open", event_id=hidden.pk))
        answers.append((response.status_code, response.content))
    assert {a for a in answers} == {answers[0]} and answers[0][0] == 403
    assert not EventRead.objects.exists()


def test_open_does_nothing_but_mark_and_redirect(team: SimpleNamespace) -> None:
    """Requirement b: the GET writes one EventRead, nothing else of this app."""
    from apps.activity.models import ActivityNotification, Event

    event = _public_event(team)
    before = (Event.objects.count(), ActivityNotification.objects.count(),
              NotificationPreference.objects.count())  # fmt: skip
    h.client_for(team.director).get(h.url(team, "open", event_id=event.pk))
    h.client_for(team.director).get(h.url(team, "open", event_id=event.pk))  # idempotent
    after = (Event.objects.count(), ActivityNotification.objects.count(),
             NotificationPreference.objects.count())  # fmt: skip
    assert before == after and EventRead.objects.count() == 1


# --------------------------------------------------------------------------- mark all read


@pytest.mark.parametrize("role", [r for r in READERS if r is not Role.PHD_STUDENT],
                         ids=lambda r: r.value)  # fmt: skip
def test_mark_all_read_is_a_post_with_csrf(team: SimpleNamespace, role: Role) -> None:
    _public_event(team)
    user = _user(team, role)
    browser = h.browser_for(user)
    assert browser.get(h.url(team, "read_all")).status_code == 405
    response = h.browser_post(browser, h.url(team, "feed"), h.url(team, "read_all"), {})
    assert response.status_code == 302
    assert EventRead.objects.filter(user=user).count() == 1


def test_mark_all_read_without_csrf_is_refused(team: SimpleNamespace) -> None:
    _public_event(team)
    browser = h.browser_for(team.director)
    assert browser.post(h.url(team, "read_all"), HTTP_ORIGIN=h.SITE_ORIGIN).status_code == 403
    assert not EventRead.objects.exists()


@pytest.mark.parametrize("role", list(h.ROLE_ATTRS), ids=lambda r: r.value)
def test_mark_all_read_on_another_thesis_is_refused(team: SimpleNamespace, role: Role) -> None:
    other = h.make_team()
    _public_event(other)
    browser = h.browser_for(_user(team, role))
    page = h.url(team, "feed") if role is not Role.GUEST else None
    if page is None:
        assert browser.get(h.url(other, "feed")).status_code == 403
        return
    response = h.browser_post(browser, page, h.url(other, "read_all"), {})
    assert response.status_code == 403
    assert not EventRead.objects.exists()


# --------------------------------------------------------------------------- preferences


@pytest.mark.parametrize("role", READERS, ids=lambda r: r.value)
def test_preferences_are_shown_and_saved(team: SimpleNamespace, role: Role) -> None:
    user = _user(team, role)
    browser = h.browser_for(user)
    page = browser.get(h.url(team, "preferences"))
    assert page.status_code == 200
    text = page.content.decode()
    # Decisions and reminders stay immediate for now (M11a): the page says so.
    assert "immédiat" in text.lower()
    response = h.browser_post(browser, h.url(team, "preferences"), h.url(team, "preferences"),
                              {"info": "FEED_ONLY"})  # fmt: skip
    assert response.status_code == 302
    saved = NotificationPreference.objects.get(user=user, thesis=team.thesis, category="INFO")
    assert saved.frequency == "FEED_ONLY"


@pytest.mark.parametrize("role", list(h.ROLE_ATTRS), ids=lambda r: r.value)
def test_preferences_of_another_thesis_are_refused(team: SimpleNamespace, role: Role) -> None:
    other = h.make_team()
    assert h.client_for(_user(team, role)).get(h.url(other, "preferences")).status_code == 403


def test_the_guest_has_no_preferences_page(team: SimpleNamespace) -> None:
    assert h.client_for(team.guest).get(h.url(team, "preferences")).status_code == 403


def test_preferences_without_csrf_are_refused(team: SimpleNamespace) -> None:
    browser = h.browser_for(team.director)
    response = browser.post(h.url(team, "preferences"), {"info": "DAILY"},
                            HTTP_ORIGIN=h.SITE_ORIGIN)  # fmt: skip
    assert response.status_code == 403
    assert not NotificationPreference.objects.exists()


def test_no_page_of_the_app_says_no_referrer(team: SimpleNamespace) -> None:
    """A page with a POST form never answers Referrer-Policy: no-referrer (CLAUDE.md)."""
    for name in ("feed", "preferences"):
        response = h.client_for(team.director).get(h.url(team, name))
        assert response.get("Referrer-Policy") != "no-referrer", name
