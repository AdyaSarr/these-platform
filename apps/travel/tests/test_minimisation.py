"""Data minimisation of the presence calendar (M7, GDPR), as an indistinguishability
test.

Two worlds share the same LIRMM days and missions to the LIRMM, and differ in
everything else: leave, Rousset, remote and conference days, days left blank, past
days corrected (journaled), missions elsewhere (Rousset, a conference, another
place, with their sealed texts). Every
surface shown to the co-supervisor must be byte-for-byte identical in both worlds:
then no function of what they see (a subtraction from a counter, a count of cells,
a CSS class, a link) can tell leave or ST days apart, since it takes the same value
in both worlds.

Surfaces: every GET page of the travel module that takes no object id (walked from
the URL configuration, so a page added later, such as the .ics export of all
missions, is covered without editing this test), each month of the year with its
navigation, the counter of each CIFRE year, and the thesis dashboard (where the
"Travel" card goes), the list of missions, the page and the edit page of each
shared mission, and the new-mission page, plain and prefilled with ?meeting= and
?submission= (shared ids, an unknown id, a malformed one: refused to the
co-supervisor, alike in both worlds). Pages with an object id are listed by
_surfaces() itself;
test_every_get_route_is_covered_or_listed checks that none is forgotten.

The test is not vacuous: the student's surfaces differ between the worlds, and the
co-supervisor's show the LIRMM days.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, resolve, reverse

from apps.conferences.models import Submission
from apps.conferences.tests.helpers import submission_of
from apps.meetings.models import Meeting
from apps.meetings.tests.helpers import meeting_of
from apps.travel import holidays, services
from apps.travel.models import Mission, PresenceDay
from apps.travel.tests.helpers import NOW, D, client_for, make_team, mission_of

pytestmark = pytest.mark.django_db(transaction=True)

FIRST, LAST = D(2026, 12, 1), D(2027, 3, 31)  # around NOW (2027-01-20)
LIRMM_DAYS = frozenset(
    {D(2026, 12, 8), D(2026, 12, 9), D(2027, 1, 5), D(2027, 1, 26), D(2027, 3, 2)}
)
MONTHS = [(2026, 9), (2026, 12), (2027, 1), (2027, 2), (2027, 3), (2027, 11)]


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    # Frozen: both worlds are rendered at the same instant.
    time_machine.move_to(NOW, tick=False)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


# --------------------------------------------------------------------------- worlds


def _world(team: SimpleNamespace, pattern: str) -> None:
    """Rewrite every non-LIRMM day of [FIRST, LAST] through the services (past days
    are journaled), then add a few days directly (weekends, a holiday)."""
    others: dict[dt.date, str | None] = {}
    cycle = {
        "a": ["ROUSSET", "LEAVE", "REMOTE", "CONFERENCE", None, "ROUSSET", "LEAVE"],
        "b": [None, None, "LEAVE", None, "ROUSSET", "REMOTE", None],
    }[pattern]
    day, i = FIRST, 0
    while day <= LAST:
        if day not in LIRMM_DAYS:
            others[day] = cycle[i % len(cycle)]
            i += 1
        day += dt.timedelta(days=1)
    PresenceDay.objects.filter(thesis=team.thesis).exclude(kind="LIRMM").delete()
    for day, kind in others.items():
        services.set_presence(
            thesis=team.thesis,
            user=team.student,
            first=day,
            last=day,
            kind=kind,
            include_non_working=True,
        )
    if pattern == "a":  # a correction of a past day: LEAVE -> ROUSSET -> LEAVE
        past = next(d for d, k in others.items() if k == "LEAVE" and d < NOW.date())
        for kind in ("ROUSSET", "LEAVE"):
            services.set_presence(
                thesis=team.thesis, user=team.student, first=past, last=past, kind=kind
            )
    # Missions elsewhere: the previous world's are cancelled (never deleted: an order
    # protects its mission, an order is history), then the pattern's own are planned.
    for old in Mission.objects.filter(thesis=team.thesis).exclude(destination="LIRMM"):
        if old.status not in ("CANCELLED", "DONE", "REIMBURSED"):
            services.cancel_mission(mission=old, user=team.student)
    elsewhere: list[dict[str, Any]] = {
        "a": [
            {"destination": "ROUSSET", "payer": "ST", "start_date": D(2027, 1, 11)},
            {"destination": "CONFERENCE", "payer": "LIRMM", "start_date": D(2027, 2, 15)},
            {
                "destination": "OTHER",
                "payer": "OTHER",
                "destination_detail": "Ville fictive",
                "start_date": D(2027, 3, 8),
            },
        ],
        "b": [{"destination": "ROUSSET", "payer": "ST", "start_date": D(2026, 12, 14)}],
    }[pattern]
    for fields in elsewhere:
        start: dt.date = fields["start_date"]
        mission = mission_of(
            team, **{**fields, "end_date": start + dt.timedelta(days=2)}, purpose=pattern
        )
        if pattern == "a" and mission.payer in ("ST", "LIRMM"):  # orders on hidden missions
            order = services.request_order(
                mission=mission, user=team.student, estimated_total="321,00"
            )
            decider = team.tutor if mission.payer == "ST" else team.director
            services.decide_order(
                order=order, user=decider, decision="GRANTED", recent_mfa=True, comment="a"
            )
            services.attach_official_order(mission=mission, user=team.student, upload=_pdf())


def _lirmm(team: SimpleNamespace) -> list[Mission]:
    for day in LIRMM_DAYS:
        assert holidays.is_working_day(day)
        PresenceDay.objects.create(thesis=team.thesis, day=day, kind="LIRMM")
    meeting_of(team)  # shared objects a mission may link (?meeting=, ?submission=)
    submission_of(team)
    shared = [  # shared missions to the LIRMM: one the co-supervisor reads, one not
        mission_of(team, payer="LIRMM", start_date=D(2027, 1, 26), end_date=D(2027, 1, 26)),
        mission_of(team, payer="ST", start_date=D(2027, 3, 2), end_date=D(2027, 3, 2)),
    ]
    services.request_order(mission=shared[1], user=team.student, estimated_total="80")
    services.attach_official_order(mission=shared[0], user=team.student, upload=_pdf())
    return shared


def _pdf() -> Any:
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile("ordre.pdf", b"%PDF-1.7\n%ordre fictif\n%%EOF\n", "application/pdf")


# --------------------------------------------------------------------------- surfaces


def _walk(patterns: list[Any], prefix: str = "") -> Iterator[tuple[str, URLPattern]]:
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p


def _plain_travel_pages(team: SimpleNamespace) -> list[str]:
    """Every GET travel route whose only argument is the thesis id."""
    out = []
    for route, pattern in _walk(get_resolver().url_patterns):
        if not route.startswith("t/<uuid:thesis_id>/travel/") or not pattern.name:
            continue
        view_class = getattr(pattern.callback, "view_class", None)
        if view_class is not None and "get" not in view_class.http_method_names:
            continue  # POST only: nothing is shown
        if set(re.findall(r"<(?:\w+:)?(\w+)>", route)) == {"thesis_id"}:
            out.append(reverse(f"travel:{pattern.name}", kwargs={"thesis_id": team.thesis.pk}))
    return sorted(out)


def _surfaces(team: SimpleNamespace) -> list[str]:
    kw = {"thesis_id": team.thesis.pk}
    months = [reverse("travel:month", kwargs={**kw, "year": y, "month": m}) for y, m in MONTHS]
    years = [reverse("travel:split", kwargs=kw) + f"?year={n}" for n in (1, 2, 3)]
    dashboard = reverse("accounts:dashboard", kwargs=kw)
    shared = Mission.objects.filter(thesis=team.thesis, destination="LIRMM").order_by("start_date")
    missions = [
        reverse(f"travel:{name}", kwargs={**kw, "mission_id": m.pk})
        for m in shared
        for name in ("mission", "edit_mission")
    ]
    meeting = Meeting.objects.filter(thesis=team.thesis).order_by("created_at").first()
    submission = Submission.objects.filter(thesis=team.thesis).order_by("created_at").first()
    new = reverse("travel:new_mission", kwargs=kw)
    prefills = [
        f"{new}?meeting={getattr(meeting, 'pk', '')}&submission={getattr(submission, 'pk', '')}",
        f"{new}?meeting=00000000-0000-4000-8000-000000000000&submission=pas-un-identifiant",
    ]
    return [*_plain_travel_pages(team), *months, *years, *missions, *prefills, dashboard]


# Pages of the student only: the co-supervisor gets a 403, the same in both worlds.
REFUSED_TO_CO_SUP = frozenset({"new_mission", "edit_mission", "orders"})

_CSRF = re.compile(r'(name="csrfmiddlewaretoken" value=|"X-CSRFToken": )"[^"]+"')


def _render(client: Client, surfaces: list[str]) -> dict[str, tuple[int, str, bytes]]:
    out = {}
    for target in surfaces:
        response = client.get(target)
        body = _CSRF.sub(r'\1"-"', response.content.decode()).encode()
        out[target] = (response.status_code, response.get("Content-Type", ""), body)
    return out


def _services(team: SimpleNamespace, user: Any) -> tuple[Any, ...]:
    return (
        services.visible_days(user, team.thesis, D(2026, 9, 1), D(2027, 11, 30)),
        [services.split_summary(user, team.thesis, cifre_year=n) for n in (1, 2, 3)],
        services.split_summary(user, team.thesis),
        [services.month_view(user, team.thesis, y, m) for y, m in MONTHS],
        [m.pk for m in services.mission_list(user, team.thesis)],
    )


# --------------------------------------------------------------------------- tests


# GET routes of the travel module NOT rendered by _surfaces(), each with its reason.
# A new GET route (with or without parameters: ids, a year, a month) must either be
# rendered in _surfaces() or be listed here, or test_every_get_route_is_covered_or_listed
# fails; an entry naming no route fails it too. Allowed while M7 is being built,
# never at the merge: tests/test_security_audit_m7.py fails unless it is empty.
DEFERRED: dict[str, str] = {}


def _get_routes() -> dict[str, str]:
    """{url name: route} of every travel route that answers GET."""
    out = {}
    for route, pattern in _walk(get_resolver().url_patterns):
        if not route.startswith("t/<uuid:thesis_id>/travel/") or not pattern.name:
            continue
        view_class = getattr(pattern.callback, "view_class", None)
        if view_class is None or "get" in view_class.http_method_names:
            out[pattern.name] = route
    return out


def test_every_get_route_is_covered_or_listed(team: SimpleNamespace) -> None:
    _lirmm(team)  # the shared missions: their pages are in _surfaces()
    covered = set()
    for target in _surfaces(team):
        match = resolve(urlsplit(target).path)
        if match.namespace == "travel":
            covered.add(match.url_name)
    routes = _get_routes()
    assert set(routes) - covered - set(DEFERRED) == set(), "render it or list it in DEFERRED"
    assert set(DEFERRED) & covered == set(), "covered now: remove it from DEFERRED"
    assert set(DEFERRED) <= set(routes), "no such route: remove it from DEFERRED"


def test_routes_with_integer_parameters_are_walked(team: SimpleNamespace) -> None:
    """The month page (<int:year>/<int:month>/) is a GET route: walked, and covered by
    the explicit months of _surfaces(), not by the parameterless walk."""
    routes = _get_routes()
    assert "<int:year>/<int:month>/" in routes["month"]
    assert {"calendar", "split", "settings", "month"} <= set(routes)
    plain = _plain_travel_pages(team)
    assert reverse("travel:calendar", kwargs={"thesis_id": team.thesis.pk}) in plain
    assert not any(resolve(p).url_name == "month" for p in plain)


def test_the_co_supervisor_cannot_tell_two_worlds_apart(team: SimpleNamespace) -> None:
    _lirmm(team)
    surfaces = _surfaces(team)
    co_sup, student = client_for(team, "co_supervisor"), client_for(team, "student")

    _world(team, "a")
    seen_a, services_a, student_a = (
        _render(co_sup, surfaces),
        _services(team, team.co_supervisor),
        _render(student, surfaces),
    )
    _world(team, "b")
    seen_b, services_b, student_b = (
        _render(co_sup, surfaces),
        _services(team, team.co_supervisor),
        _render(student, surfaces),
    )

    # Not vacuous: the worlds differ for the student, the pages work, LIRMM days show.
    assert student_a != student_b
    statuses = {k: v[0] for k, v in seen_a.items()}
    refused = {k for k, v in statuses.items() if v != 200}
    # Only the student's forms are refused (403); every other page is shown.
    assert {resolve(urlsplit(k).path).url_name for k in refused} <= REFUSED_TO_CO_SUP, statuses
    assert all(statuses[k] == 403 for k in refused), statuses
    january = reverse(
        "travel:month", kwargs={"thesis_id": team.thesis.pk, "year": 2027, "month": 1}
    )
    assert b"LIRMM" in seen_a[january][2]
    assert services_a[0] == {d: "LIRMM" for d in LIRMM_DAYS}

    assert services_a == services_b
    for target in surfaces:
        assert seen_a[target] == seen_b[target], target


def test_the_director_cannot_tell_leave_from_any_other_absence_label(
    team: SimpleNamespace,
) -> None:
    """The director sees "unavailable" where the student sees leave: two worlds that
    differ only in the WORD behind an unavailable day (there is only LEAVE today, so
    the check is that the word, its code and its CSS class never reach the page)."""
    _lirmm(team)
    _world(team, "a")
    director = _render(client_for(team, "director"), _surfaces(team))
    for target, (_, _, body) in director.items():
        for word in (b"LEAVE", b"Cong\xc3\xa9", b"cong\xc3\xa9", b"Leave", b"leave"):
            assert word not in body, (target, word)
    january = reverse(
        "travel:month", kwargs={"thesis_id": team.thesis.pk, "year": 2027, "month": 1}
    )
    assert b"Indisponible" in director[january][2]
