"""Data minimisation of the presence calendar (M7, GDPR), as an indistinguishability
test.

Two worlds share the same LIRMM days and differ in everything else: leave, Rousset,
remote and conference days, days left blank, past days corrected (journaled). Every
surface shown to the co-supervisor must be byte-for-byte identical in both worlds:
then no function of what they see (a subtraction from a counter, a count of cells,
a CSS class, a link) can tell leave or ST days apart, since it takes the same value
in both worlds.

Surfaces: every GET page of the travel module that takes no object id (walked from
the URL configuration, so a page added later, such as the .ics export of all
missions, is covered without editing this test), each month of the year with its
navigation, the counter of each CIFRE year, and the thesis dashboard (where the
"Travel" card goes). The .ics of one mission and the mission pages are added with
the missions (step 3 onwards): they take an id.

The test is not vacuous: the student's surfaces differ between the worlds, and the
co-supervisor's show the LIRMM days.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from apps.travel import holidays, services
from apps.travel.models import PresenceDay
from apps.travel.tests.helpers import NOW, D, client_for, make_team

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


def _lirmm(team: SimpleNamespace) -> None:
    for day in LIRMM_DAYS:
        assert holidays.is_working_day(day)
        PresenceDay.objects.create(thesis=team.thesis, day=day, kind="LIRMM")


# --------------------------------------------------------------------------- surfaces


def _walk(patterns: list[Any], prefix: str = "") -> Iterator[tuple[str, URLPattern]]:
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p


def _plain_travel_pages(team: SimpleNamespace) -> list[str]:
    """Every travel route whose only argument is the thesis id."""
    out = []
    for route, pattern in _walk(get_resolver().url_patterns):
        if not route.startswith("t/<uuid:thesis_id>/travel/") or not pattern.name:
            continue
        if set(re.findall(r"<(?:\w+:)?(\w+)>", route)) == {"thesis_id"}:
            out.append(reverse(f"travel:{pattern.name}", kwargs={"thesis_id": team.thesis.pk}))
    return sorted(out)


def _surfaces(team: SimpleNamespace) -> list[str]:
    kw = {"thesis_id": team.thesis.pk}
    months = [reverse("travel:month", kwargs={**kw, "year": y, "month": m}) for y, m in MONTHS]
    years = [reverse("travel:split", kwargs=kw) + f"?year={n}" for n in (1, 2, 3)]
    dashboard = reverse("accounts:dashboard", kwargs=kw)
    return [*_plain_travel_pages(team), *months, *years, dashboard]


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
    )


# --------------------------------------------------------------------------- tests


def test_surfaces_include_the_calendar_and_the_counter(team: SimpleNamespace) -> None:
    pages = _plain_travel_pages(team)
    assert reverse("travel:calendar", kwargs={"thesis_id": team.thesis.pk}) in pages
    assert reverse("travel:split", kwargs={"thesis_id": team.thesis.pk}) in pages


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
    assert all(status == 200 for status, _, _ in seen_a.values()), {
        k: v[0] for k, v in seen_a.items()
    }
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
