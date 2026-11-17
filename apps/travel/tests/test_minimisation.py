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
NEAR_HIDDEN = [D(2027, 1, d) for d in (21, 22, 25, 27, 28, 29)] + [
    D(2027, 2, d) for d in (1, 2, 3, 4)
]


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
            team,
            **{**fields, "end_date": start + dt.timedelta(days=2)},
            purpose=pattern,
            # Hidden missions linked to the shared meeting and submission: their M5 and
            # M6 pages must not show them to the co-supervisor.
            meeting=Meeting.objects.filter(thesis=team.thesis).order_by("created_at").first(),
            submission=Submission.objects.filter(thesis=team.thesis).order_by("created_at").first(),
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
        if pattern == "a":  # expenses and receipts on hidden missions
            line = services.add_expense(
                mission=mission, user=team.student, category="LODGING", estimated="210"
            )
            services.attach_receipt(expense=line, user=team.student, upload=_pdf())
    if pattern == "a":
        # Hidden missions NEARER than the shared one of 12 February, more than the
        # 8 lines of the « À venir » card: a cut before the filter per viewer would
        # change what the co-supervisor's card shows.
        for day in NEAR_HIDDEN:
            mission_of(
                team, destination="ROUSSET", payer="ST", start_date=day, end_date=day,
                purpose=pattern,
            )  # fmt: skip
        # Documents the co-supervisor does not read (INTERNAL_ST), more recent than
        # every other one: the documents card cuts to 3 after the filter per viewer
        # (frontend step 8b), so theirs must not change.
        from apps.core.confidentiality import ConfidentialityLevel
        from apps.vault import services as vault
        from apps.vault.models import Document

        for n in range(4):
            hidden = vault.store(
                thesis=team.thesis,
                upload=_pdf(),
                title=f"Document caché fictif {n}",
                level=ConfidentialityLevel.INTERNAL_ST,
                user=team.student,
            )
            Document.objects.filter(pk=hidden.pk).update(
                updated_at=NOW + dt.timedelta(hours=1, minutes=n)
            )


def _lirmm(team: SimpleNamespace) -> list[Mission]:
    for day in LIRMM_DAYS:
        assert holidays.is_working_day(day)
        PresenceDay.objects.create(thesis=team.thesis, day=day, kind="LIRMM")
    services.set_rules(  # the monthly rule's text ("in progress", "not met") is rendered
        thesis=team.thesis, user=team.director, recent_mfa=True, min_lab_days_per_month=3
    )
    meeting_of(team)  # shared objects a mission may link (?meeting=, ?submission=)
    submission_of(team)
    shared = [  # shared missions to the LIRMM: one the co-supervisor reads, one not
        mission_of(team, payer="LIRMM", start_date=D(2027, 1, 26), end_date=D(2027, 1, 26)),
        mission_of(team, payer="ST", start_date=D(2027, 3, 2), end_date=D(2027, 3, 2)),
    ]
    services.request_order(mission=shared[1], user=team.student, estimated_total="80")
    services.attach_official_order(mission=shared[0], user=team.student, upload=_pdf())
    line = services.add_expense(
        mission=shared[0], user=team.student, category="MEALS", flat_rate=True, estimated="30"
    )
    services.attach_receipt(expense=line, user=team.student, upload=_pdf())
    # « À venir » card of the dashboard (frontend step 8a): a shared mission the
    # co-supervisor sees, after the hidden ones of world "a" (_world).
    shared.append(
        mission_of(team, payer="LIRMM", start_date=D(2027, 2, 12), end_date=D(2027, 2, 12))
    )
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
        for name in ("mission", "edit_mission", "mission_ics")
    ]
    meeting = Meeting.objects.filter(thesis=team.thesis).order_by("created_at").first()
    submission = Submission.objects.filter(thesis=team.thesis).order_by("created_at").first()
    new = reverse("travel:new_mission", kwargs=kw)
    prefills = [
        f"{new}?meeting={getattr(meeting, 'pk', '')}&submission={getattr(submission, 'pk', '')}",
        f"{new}?meeting=00000000-0000-4000-8000-000000000000&submission=pas-un-identifiant",
    ]
    linked = []  # the M5 and M6 pages that show a "Travel" section
    if meeting is not None:
        linked.append(reverse("meetings:detail", kwargs={**kw, "meeting_id": meeting.pk}))
    if submission is not None:
        linked.append(
            reverse("conferences:submission", kwargs={**kw, "submission_id": submission.pk})
        )
    return [*_plain_travel_pages(team), *months, *years, *missions, *prefills, *linked, dashboard]


# Pages of the student only: the co-supervisor gets a 403, the same in both worlds.
REFUSED_TO_CO_SUP = frozenset({"new_mission", "edit_mission", "orders", "budget"})

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

    # Module tabs (apps/ui/tabs.py, frontend step 2): neither the budget nor the
    # approvals queue for the co-supervisor, on any surface; each one shown to
    # whoever it is for, or this check would be vacuous.
    budget = reverse("travel:budget", kwargs={"thesis_id": team.thesis.pk}).encode()
    orders = reverse("travel:orders", kwargs={"thesis_id": team.thesis.pk}).encode()
    bars = [_tab_bar(body) for status, _type, body in seen_a.values() if status == 200]
    tabbed = [nav for nav in bars if nav is not None]
    assert tabbed
    assert not any(budget in nav or orders in nav for nav in tabbed)
    calendar = reverse("travel:calendar", kwargs={"thesis_id": team.thesis.pk})
    student_tabs = _tab_bar(student_a[calendar][2])
    assert student_tabs is not None
    assert budget in student_tabs and orders not in student_tabs
    for decider in ("director", "tutor"):
        tabs = _tab_bar(client_for(team, decider).get(calendar).content)
        assert tabs is not None, decider
        assert orders in tabs and budget in tabs, decider

    # « À venir » card: cut after the filter per viewer. The student's card is full
    # of the hidden missions; the co-supervisor's shows theirs, 12 February included.
    dashboard = reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk})
    student_agenda = _agenda_card(student_a[dashboard][2])
    assert student_agenda.count(b'class="agenda-item"') == 8
    assert "12\u00a0févr.".encode() not in student_agenda
    co_sup_agenda = _agenda_card(seen_a[dashboard][2])
    assert "12\u00a0févr.".encode() in co_sup_agenda
    assert co_sup_agenda.count(b'class="agenda-item"') == 2  # 26 January, 12 February

    # Cards of step 8b (documents, team, conferences): the co-supervisor's are in the
    # bytes compared above, identical in both worlds; not vacuous: the student's
    # documents card counts what the co-supervisor does not read.
    student_docs = _dashboard_card(student_a[dashboard][2], b"documents")
    co_sup_docs = _dashboard_card(seen_a[dashboard][2], b"documents")
    assert student_docs != co_sup_docs
    # The cut to the 3 latest documents comes after the filter per viewer: the
    # student's latest ones (receipts and orders of hidden missions, stored after
    # the shared ones) are not on the co-supervisor's card, which still lists theirs.
    links = re.compile(rb"documents/([0-9a-f-]{36})/")
    student_latest = links.findall(student_docs)
    co_sup_latest = links.findall(co_sup_docs)
    assert len(student_latest) == 3 and co_sup_latest
    assert not set(student_latest) & set(co_sup_latest)
    for key in (b"team", b"conferences"):
        assert _dashboard_card(seen_a[dashboard][2], key) == _dashboard_card(
            seen_b[dashboard][2], key
        )


def _dashboard_card(body: bytes, key: bytes) -> bytes:
    match = re.search(
        rb'<section class="block" data-block="' + key + rb'">.*?</section>', body, re.S
    )
    assert match is not None, key
    return match.group(0)


def _agenda_card(body: bytes) -> bytes:
    match = re.search(rb'<section class="card agenda".*?</section>', body, re.S)
    assert match is not None
    return match.group(0)


def _tab_bar(body: bytes) -> bytes | None:
    match = re.search(rb'<nav class="tabs".*?</nav>', body, re.S)
    return match.group(0) if match else None


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
