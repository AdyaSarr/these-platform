"""The ST twin of the two-worlds test (test_minimisation.py), for the company
co-supervisor (ST_ONLY).

Two worlds share the same Rousset days and missions to Rousset, and differ in
everything else: LIRMM, leave, remote and conference days, days left blank, past
days corrected (journaled), missions elsewhere (the LIRMM, a conference, another
place, with their orders, receipts and sealed texts), documents at
INTERNAL_LIRMM. Every surface shown to the company co-supervisor must be
byte-for-byte identical in both worlds: then nothing they see (a counter, a count
of cells, a CSS class, a link) can tell the
hidden days apart.

Same surfaces as the LIRMM test, walked from the URL configuration; the test is
not vacuous: the student's surfaces differ between the worlds, and the company
co-supervisor's show the Rousset days.
"""

from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from django.urls import resolve, reverse

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory
from apps.conferences.models import Submission
from apps.conferences.tests.helpers import submission_of
from apps.meetings.models import Meeting
from apps.meetings.tests.helpers import meeting_of
from apps.travel import holidays, services
from apps.travel.models import Mission, PresenceDay
from apps.travel.tests.helpers import NOW, PASSWORD, D, client_for, make_team, mission_of
from apps.travel.tests.test_minimisation import (
    DEFERRED,
    FIRST,
    LAST,
    MONTHS,
    NEAR_HIDDEN,
    REFUSED_TO_CO_SUP,
    _agenda_card,
    _dashboard_card,
    _get_routes,
    _pdf,
    _plain_travel_pages,
    _render,
    _services,
    _tab_bar,
)

pytestmark = pytest.mark.django_db(transaction=True)

ST_DAYS = frozenset({D(2026, 12, 8), D(2026, 12, 9), D(2027, 1, 5), D(2027, 1, 26), D(2027, 3, 2)})


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=False)  # both worlds are rendered at the same instant


@pytest.fixture
def team() -> SimpleNamespace:
    team = make_team()
    team.st_co_supervisor = MembershipFactory.create(
        thesis=team.thesis, role=Role.INDUSTRIAL_CO_SUPERVISOR, user__password=PASSWORD
    ).user
    return team


# --------------------------------------------------------------------------- worlds


def _rousset(team: SimpleNamespace) -> list[Mission]:
    """What both worlds share: Rousset days, missions to Rousset, the rules."""
    for day in ST_DAYS:
        assert holidays.is_working_day(day)
        PresenceDay.objects.create(thesis=team.thesis, day=day, kind="ROUSSET")
    services.set_rules(  # the monthly LIRMM rule is set (not shown to this viewer)
        thesis=team.thesis, user=team.director, recent_mfa=True, min_lab_days_per_month=3
    )
    meeting_of(team)
    submission_of(team)
    shared = [  # one the company co-supervisor reads (ST), one not (LIRMM)
        mission_of(
            team, destination="ROUSSET", payer="ST",
            start_date=D(2027, 1, 26), end_date=D(2027, 1, 26),
        ),
        mission_of(
            team, destination="ROUSSET", payer="LIRMM",
            start_date=D(2027, 3, 2), end_date=D(2027, 3, 2),
        ),
    ]  # fmt: skip
    services.request_order(mission=shared[1], user=team.student, estimated_total="80")
    services.attach_official_order(mission=shared[0], user=team.student, upload=_pdf())
    line = services.add_expense(
        mission=shared[0], user=team.student, category="MEALS", flat_rate=True, estimated="30"
    )
    services.attach_receipt(expense=line, user=team.student, upload=_pdf())
    shared.append(
        mission_of(
            team, destination="ROUSSET", payer="ST",
            start_date=D(2027, 2, 12), end_date=D(2027, 2, 12),
        )
    )  # fmt: skip
    return shared


def _world(team: SimpleNamespace, pattern: str) -> None:
    """Rewrite every non-Rousset day of [FIRST, LAST] through the services (past days
    are journaled), then the missions elsewhere and the hidden documents."""
    cycle = {
        "a": ["LIRMM", "LEAVE", "REMOTE", "CONFERENCE", None, "LIRMM", "LEAVE"],
        "b": [None, None, "LEAVE", None, "LIRMM", "REMOTE", None],
    }[pattern]
    others: dict[dt.date, str | None] = {}
    day, i = FIRST, 0
    while day <= LAST:
        if day not in ST_DAYS:
            others[day] = cycle[i % len(cycle)]
            i += 1
        day += dt.timedelta(days=1)
    PresenceDay.objects.filter(thesis=team.thesis).exclude(kind="ROUSSET").delete()
    for day, kind in others.items():
        services.set_presence(
            thesis=team.thesis, user=team.student, first=day, last=day, kind=kind,
            include_non_working=True,
        )  # fmt: skip
    if pattern == "a":  # a correction of a past day: LEAVE -> LIRMM -> LEAVE
        past = next(d for d, k in others.items() if k == "LEAVE" and d < NOW.date())
        for kind in ("LIRMM", "LEAVE"):
            services.set_presence(
                thesis=team.thesis, user=team.student, first=past, last=past, kind=kind
            )
    for old in Mission.objects.filter(thesis=team.thesis).exclude(destination="ROUSSET"):
        if old.status not in ("CANCELLED", "DONE", "REIMBURSED"):
            services.cancel_mission(mission=old, user=team.student)
    elsewhere: list[dict[str, Any]] = {
        "a": [
            {"destination": "LIRMM", "payer": "LIRMM", "start_date": D(2027, 1, 11)},
            {"destination": "LIRMM", "payer": "ST", "start_date": D(2027, 2, 1)},
            {"destination": "CONFERENCE", "payer": "LIRMM", "start_date": D(2027, 2, 15)},
            {
                "destination": "OTHER",
                "payer": "OTHER",
                "destination_detail": "Ville fictive",
                "start_date": D(2027, 3, 8),
            },
        ],
        "b": [{"destination": "LIRMM", "payer": "LIRMM", "start_date": D(2026, 12, 14)}],
    }[pattern]
    for fields in elsewhere:
        start: dt.date = fields["start_date"]
        mission = mission_of(
            team,
            **{**fields, "end_date": start + dt.timedelta(days=2)},
            purpose=pattern,
            meeting=Meeting.objects.filter(thesis=team.thesis).order_by("created_at").first(),
            submission=Submission.objects.filter(thesis=team.thesis).order_by("created_at").first(),
        )
        if pattern == "a" and mission.payer in ("ST", "LIRMM"):
            order = services.request_order(
                mission=mission, user=team.student, estimated_total="321,00"
            )
            decider = team.tutor if mission.payer == "ST" else team.director
            services.decide_order(
                order=order, user=decider, decision="GRANTED", recent_mfa=True, comment="a"
            )
            services.attach_official_order(mission=mission, user=team.student, upload=_pdf())
        if pattern == "a":
            line = services.add_expense(
                mission=mission, user=team.student, category="LODGING", estimated="210"
            )
            services.attach_receipt(expense=line, user=team.student, upload=_pdf())
    if pattern == "a":
        # Hidden missions nearer than the shared one of 12 February, more than the 8
        # lines of the « À venir » card: the cut comes after the filter per viewer.
        for day in NEAR_HIDDEN:
            mission_of(
                team, destination="LIRMM", payer="LIRMM", start_date=day, end_date=day,
                purpose=pattern,
            )  # fmt: skip
        # Documents this viewer does not read (INTERNAL_LIRMM), the most recent ones.
        from apps.core.confidentiality import ConfidentialityLevel
        from apps.vault import services as vault
        from apps.vault.models import Document

        for n in range(4):
            hidden = vault.store(
                thesis=team.thesis,
                upload=_pdf(),
                title=f"Document caché fictif {n}",
                level=ConfidentialityLevel.INTERNAL_LIRMM,
                user=team.student,
            )
            Document.objects.filter(pk=hidden.pk).update(
                updated_at=NOW + dt.timedelta(hours=1, minutes=n)
            )


# --------------------------------------------------------------------------- surfaces


def _surfaces(team: SimpleNamespace) -> list[str]:
    kw = {"thesis_id": team.thesis.pk}
    months = [reverse("travel:month", kwargs={**kw, "year": y, "month": m}) for y, m in MONTHS]
    years = [reverse("travel:split", kwargs=kw) + f"?year={n}" for n in (1, 2, 3)]
    dashboard = reverse("accounts:dashboard", kwargs=kw)
    shared = Mission.objects.filter(thesis=team.thesis, destination="ROUSSET").order_by(
        "start_date"
    )
    missions = [
        reverse(f"travel:{name}", kwargs={**kw, "mission_id": m.pk})
        for m in shared
        for name in ("mission", "edit_mission", "mission_ics")
    ]
    meeting = Meeting.objects.filter(thesis=team.thesis).order_by("created_at").first()
    submission = Submission.objects.filter(thesis=team.thesis).order_by("created_at").first()
    new = reverse("travel:new_mission", kwargs=kw)
    prefills = [
        f"{new}?meeting={getattr(meeting, 'pk', '')}&submission={getattr(submission, 'pk', '')}",
        f"{new}?meeting=00000000-0000-4000-8000-000000000000&submission=pas-un-identifiant",
    ]
    linked = []
    if meeting is not None:
        linked.append(reverse("meetings:detail", kwargs={**kw, "meeting_id": meeting.pk}))
    if submission is not None:
        linked.append(
            reverse("conferences:submission", kwargs={**kw, "submission_id": submission.pk})
        )
    return [*_plain_travel_pages(team), *months, *years, *missions, *prefills, *linked, dashboard]


# --------------------------------------------------------------------------- tests


def test_every_get_route_is_covered_or_listed(team: SimpleNamespace) -> None:
    _rousset(team)
    covered = set()
    for target in _surfaces(team):
        match = resolve(urlsplit(target).path)
        if match.namespace == "travel":
            covered.add(match.url_name)
    assert set(_get_routes()) - covered - set(DEFERRED) == set()


def test_the_company_co_supervisor_cannot_tell_two_worlds_apart(team: SimpleNamespace) -> None:
    _rousset(team)
    surfaces = _surfaces(team)
    viewer = client_for(team, "st_co_supervisor")
    student = client_for(team, "student")

    _world(team, "a")
    seen_a, services_a, student_a = (
        _render(viewer, surfaces),
        _services(team, team.st_co_supervisor),
        _render(student, surfaces),
    )
    _world(team, "b")
    seen_b, services_b, student_b = (
        _render(viewer, surfaces),
        _services(team, team.st_co_supervisor),
        _render(student, surfaces),
    )

    # Not vacuous: the worlds differ for the student, the pages work, Rousset shows.
    assert student_a != student_b
    statuses = {k: v[0] for k, v in seen_a.items()}
    refused = {k for k, v in statuses.items() if v != 200}
    assert {resolve(urlsplit(k).path).url_name for k in refused} <= REFUSED_TO_CO_SUP, statuses
    assert all(statuses[k] == 403 for k in refused), statuses
    january = reverse(
        "travel:month", kwargs={"thesis_id": team.thesis.pk, "year": 2027, "month": 1}
    )
    assert b"Rousset" in seen_a[january][2]
    assert services_a[0] == {d: "ROUSSET" for d in ST_DAYS}

    assert services_a == services_b
    for target in surfaces:
        assert seen_a[target] == seen_b[target], target

    # Neither the budget nor the approvals tab, on any surface.
    budget = reverse("travel:budget", kwargs={"thesis_id": team.thesis.pk}).encode()
    orders = reverse("travel:orders", kwargs={"thesis_id": team.thesis.pk}).encode()
    bars = [_tab_bar(body) for status, _type, body in seen_a.values() if status == 200]
    tabbed = [nav for nav in bars if nav is not None]
    assert tabbed
    assert not any(budget in nav or orders in nav for nav in tabbed)

    # « À venir »: cut after the filter per viewer.
    dashboard = reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk})
    student_agenda = _agenda_card(student_a[dashboard][2])
    assert student_agenda.count(b'class="agenda-item"') == 8
    assert "12\u00a0févr.".encode() not in student_agenda
    viewer_agenda = _agenda_card(seen_a[dashboard][2])
    assert "12\u00a0févr.".encode() in viewer_agenda
    assert viewer_agenda.count(b'class="agenda-item"') == 2  # 26 January, 12 February

    # Documents card: the cut to 3 comes after the filter per viewer.
    student_docs = _dashboard_card(student_a[dashboard][2], b"documents")
    viewer_docs = _dashboard_card(seen_a[dashboard][2], b"documents")
    links = re.compile(rb"documents/([0-9a-f-]{36})/")
    student_latest = links.findall(student_docs)
    viewer_latest = links.findall(viewer_docs)
    assert len(student_latest) == 3 and viewer_latest
    assert not set(student_latest) & set(viewer_latest)
