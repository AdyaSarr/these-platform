"""The « À venir » card of the thesis dashboard: what happens in the next 30 days.

Order of the operations, never another one:

1. each source reads only what THIS viewer may see: the module's action (the same
   one as its card), then the module's own filter (Mission.objects.visible_to,
   admin_file.services.visible_items...); the 30-day window may be part of the same
   query, a conjunction whose order does not matter;
2. the window, the sort, and only then the cut at MAX_ENTRIES (select()).

A cut before step 1 would let the number of lines tell hidden elements apart
(apps/ui/tests/test_agenda.py, apps/travel/tests/test_minimisation.py).

No sealed text: dates, kinds, acronyms, destinations, categories and item labels
only. The dashboard opens sealed texts once, for the Meetings card
(apps/ui/tests/test_dashboard_cards.py).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from apps.accounts.permissions import Action

from .navigation import allowed

WINDOW_DAYS = 30
MAX_ENTRIES = 8
# Same day and time: this order, then the label.
KINDS = ("meeting", "deadline", "request", "mission", "objective", "item")


@dataclass(frozen=True)
class Entry:
    day: dt.date
    time: dt.time | None
    kind: str
    icon: str
    label: str
    detail: str
    url: str


def select(entries: Iterable[Entry], today: dt.date) -> list[Entry]:
    """``entries`` are already filtered per viewer: window, sort, then cut."""
    last = today + dt.timedelta(days=WINDOW_DAYS)
    within = [e for e in entries if today <= e.day <= last]
    within.sort(key=lambda e: (e.day, e.time or dt.time.min, KINDS.index(e.kind), e.label))
    return within[:MAX_ENTRIES]


def agenda_for(
    request: HttpRequest, thesis: Any, blocks: list[tuple[Any, dict[str, Any]]]
) -> list[Entry]:
    actions = allowed(request)
    today = timezone.localdate()
    last = today + dt.timedelta(days=WINDOW_DAYS)
    data = {block.key: block_data for block, block_data in blocks}
    entries: list[Entry] = []
    if Action.MEETING_READ in actions:
        entries += _meetings(request.user, thesis, today, last)
    if Action.CONFERENCE_READ in actions and "conferences" in data:
        entries += _conferences(thesis, data["conferences"])
    if Action.TRAVEL_READ in actions:
        entries += _missions(request.user, thesis, today, last)
    if Action.OBJECTIVE_READ in actions:
        entries += _objectives(thesis, today, last)
    if Action.CIFRE_FILE_READ in actions:
        entries += _items(request.user, thesis, today, last)
    return select(entries, today)


def countdown(days: int) -> str:
    """« aujourd'hui », « demain », « dans 21 jours »: ``days`` counted in Paris
    dates (conferences.services.dashboard_block, timezone.localdate())."""
    if days == 0:
        return _("today")
    if days == 1:
        return _("tomorrow")
    return ngettext("in %(count)d day", "in %(count)d days", days) % {"count": days}


def _start_of(day: dt.date) -> dt.datetime:
    return timezone.make_aware(dt.datetime.combine(day, dt.time()))


def _meetings(user: Any, thesis: Any, today: dt.date, last: dt.date) -> list[Entry]:
    """Every scheduled meeting of the thesis (MEETING_READ shows the list of all
    of them, meetings.services.meeting_list); the title is never opened."""
    from apps.meetings.models import Attendee, Meeting

    meetings = list(
        Meeting.objects.filter(
            thesis=thesis,
            status=Meeting.Status.SCHEDULED,
            starts_at__gte=_start_of(today),
            starts_at__lt=_start_of(last + dt.timedelta(days=1)),
        )
    )
    mine = set(
        Attendee.objects.filter(meeting__in=meetings, user_id=user.pk).values_list(
            "meeting_id", flat=True
        )
    )
    out = []
    for m in meetings:
        local = timezone.localtime(m.starts_at)
        out.append(
            Entry(
                day=local.date(),
                time=local.time(),
                kind="meeting",
                icon="calendar-days",
                label=str(m.get_kind_display()),
                detail="" if m.pk in mine else _("you do not attend"),
                url=reverse("meetings:detail", kwargs={"thesis_id": thesis.pk, "meeting_id": m.pk}),
            )
        )
    return out


def _conferences(thesis: Any, data: dict[str, Any]) -> list[Entry]:
    """From the Conferences card's data (conferences.services.dashboard_block): the
    deadlines of the targeted editions and the dates to request ST's approval."""
    out = []
    for d in data.get("deadlines", []):
        deadline = d.deadline
        out.append(
            Entry(
                day=d.paris[0],
                time=d.paris[1],
                kind="deadline",
                icon="presentation",
                label=f"{deadline.edition.venue.acronym} {deadline.edition.year}",
                detail=str(deadline.get_kind_display()),
                url=reverse(
                    "conferences:edition",
                    kwargs={"thesis_id": thesis.pk, "edition_id": deadline.edition_id},
                ),
            )
        )
    for submission, due, days in data.get("request_dues", []):
        out.append(
            Entry(
                day=due,
                time=None,
                kind="request",
                icon="clock",
                label=_("Request ST's approval"),
                detail=f"{submission.venue.acronym} · {countdown(days)}",
                url=reverse("conferences:submissions", kwargs={"thesis_id": thesis.pk}),
            )
        )
    return out


def _missions(user: Any, thesis: Any, today: dt.date, last: dt.date) -> list[Entry]:
    from apps.travel.choices import MissionStatus
    from apps.travel.models import Mission

    missions = (
        Mission.objects.visible_to(user, thesis)
        .filter(start_date__gte=today, start_date__lte=last)
        .exclude(status=MissionStatus.CANCELLED)
    )
    return [
        Entry(
            day=m.start_date,
            time=None,
            kind="mission",
            icon="plane",
            label=str(m.get_destination_display()),
            detail=str(m.get_status_display()),
            url=reverse("travel:mission", kwargs={"thesis_id": thesis.pk, "mission_id": m.pk}),
        )
        for m in missions
    ]


def _objectives(thesis: Any, today: dt.date, last: dt.date) -> list[Entry]:
    """Every objective of the thesis is listed to OBJECTIVE_READ (its title is
    sealed): the category only."""
    from apps.objectives.models import Objective, Status

    objectives = Objective.objects.filter(
        thesis=thesis,
        status__in=(Status.PLANNED, Status.IN_PROGRESS),
        due_date__gte=today,
        due_date__lte=last,
    )
    return [
        Entry(
            day=o.due_date,
            time=None,
            kind="objective",
            icon="target",
            label=_("Objective due"),
            detail=str(o.get_category_display()),
            url=reverse("objectives:detail", kwargs={"thesis_id": thesis.pk, "objective_id": o.pk}),
        )
        for o in objectives
    ]


def _items(user: Any, thesis: Any, today: dt.date, last: dt.date) -> list[Entry]:
    """The items this viewer sees (the tutor: the CIFRE part), not validated yet."""
    from apps.admin_file import services as admin_file
    from apps.admin_file.models import ItemStatus

    items = (
        admin_file.visible_items(user, thesis)
        .exclude(status=ItemStatus.VALIDATED)
        .filter(due_date__gte=today, due_date__lte=last)
    )
    return [
        Entry(
            day=item.due_date,
            time=None,
            kind="item",
            icon="folder-open",
            label=admin_file.label_of(item),
            detail=str(item.get_status_display()),
            url=reverse("admin_file:item", kwargs={"thesis_id": thesis.pk, "item_id": item.pk}),
        )
        for item in items
        if item.due_date is not None  # filtered by the query: for the type checker
    ]
