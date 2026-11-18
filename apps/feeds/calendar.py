"""The content of a personal iCal feed (M12, docs/modules/M12-ical.md).

Only what the person reads (the existing rules, option B), narrowed to what
concerns them: the meetings they are invited to, the M3 pieces they file or
validate, the M6 deadlines of the submissions they read (their type only), the
missions they see, the due dates of the objectives they read. Generic labels or
clear metadata only: never a sealed text, never a label typed by someone, never a
conference, a level, an amount or a person. Times in UTC, due dates all-day.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from apps.accounts.permissions import roles_of
from apps.accounts.roles import Role
from apps.core import ics

from .models import CalendarFeed

BEFORE = dt.timedelta(days=30)
AFTER = dt.timedelta(days=365)


def _host() -> str:
    return urlsplit(settings.SITE_URL).hostname or "localhost"


def _url(name: str, **kwargs: Any) -> str:
    return str(settings.SITE_URL + reverse(name, kwargs=kwargs))


def _all_day(uid: str, day: dt.date, summary: str, url: str, now: dt.datetime,
             status: str = "CONFIRMED", sequence: int = 0) -> list[str]:  # fmt: skip
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}@{_host()}",
        f"DTSTAMP:{ics.utc(now)}",
        f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
        f"DTEND;VALUE=DATE:{day + dt.timedelta(days=1):%Y%m%d}",
        f"SEQUENCE:{sequence}",
        f"STATUS:{status}",
        "TRANSP:TRANSPARENT",
        f"SUMMARY:{ics.text(summary)}",
        f"DESCRIPTION:{ics.text(_('Details on the platform:') + ' ' + url)}",
        f"URL:{url}",
        "END:VEVENT",
    ]


def _meetings(user: Any, thesis: Any, now: dt.datetime) -> list[tuple[Any, list[str]]]:
    from apps.meetings.ics import summary
    from apps.meetings.models import Attendee, Meeting

    ids = Attendee.objects.filter(user_id=user.pk, meeting__thesis=thesis).values("meeting_id")
    # The rule of the lists (SR-01): MEETING_READ and the level, readable_by.
    rows = Meeting.objects.readable_by(user, thesis).filter(
        pk__in=ids, starts_at__gte=now - BEFORE, starts_at__lte=now + AFTER
    )
    out = []
    for meeting in rows:
        url = _url("meetings:detail", thesis_id=thesis.pk, meeting_id=meeting.pk)
        lines = [
            "BEGIN:VEVENT",
            f"UID:{meeting.pk}@{_host()}",  # the UID of the M5a export
            f"DTSTAMP:{ics.utc(now)}",
            f"DTSTART:{ics.utc(meeting.starts_at)}",
            f"DTEND:{ics.utc(meeting.ends_at)}",
            f"SEQUENCE:{meeting.ics_sequence}",
            f"STATUS:{'CANCELLED' if meeting.is_cancelled else 'CONFIRMED'}",
            f"SUMMARY:{ics.text(summary(meeting))}",
            f"LOCATION:{ics.text(str(meeting.get_location_display()))}",
            f"DESCRIPTION:{ics.text(_('Details on the platform:') + ' ' + url)}",
            f"URL:{url}",
            "END:VEVENT",
        ]
        out.append((meeting.starts_at, lines))
    return out


def _items(user: Any, thesis: Any, now: dt.datetime) -> list[tuple[Any, list[str]]]:
    from apps.admin_file.models import ItemApproval, ItemStatus
    from apps.admin_file.services import visible_items

    today = timezone.localdate(now)
    held = roles_of(user, thesis)
    items = visible_items(user, thesis).filter(
        due_date__gte=today - BEFORE, due_date__lte=today + AFTER
    )
    out = []
    for item in items:
        mine = None
        if Role.PHD_STUDENT in held and item.status in (ItemStatus.TODO, ItemStatus.REJECTED):
            mine = _("Administrative file: a piece to file")
        elif (
            item.status == ItemStatus.SUBMITTED
            and item.submitted_by_id != user.pk
            and any(Role(r) in held for r in item.validator_roles)
            and not ItemApproval.objects.filter(
                item=item, cycle=item.cycle, user_id=user.pk
            ).exists()
        ):
            mine = _("Administrative file: a piece to validate")
        if mine is None or item.due_date is None:
            continue
        url = _url("admin_file:item", thesis_id=thesis.pk, item_id=item.pk)
        start = dt.datetime.combine(item.due_date, dt.time(0), tzinfo=dt.UTC)
        out.append((start, _all_day(f"item-{item.pk}", item.due_date, mine, url, now)))
    return out


def _deadlines(user: Any, thesis: Any, now: dt.datetime) -> list[tuple[Any, list[str]]]:
    from apps.conferences.models import Deadline, Submission

    readable = [
        s
        for s in Submission.objects.readable_by(user, thesis).filter(edition__isnull=False)
        if not s.is_terminal
    ]
    by_edition: dict[Any, Any] = {}
    for submission in sorted(readable, key=lambda s: (s.created_at, s.pk)):
        by_edition.setdefault(submission.edition_id, submission)
    deadlines = Deadline.objects.filter(
        edition_id__in=list(by_edition), cancelled_at__isnull=True,
        due_at__gte=now - BEFORE, due_at__lte=now + AFTER,
    )  # fmt: skip
    out = []
    for deadline in deadlines:
        submission = by_edition[deadline.edition_id]
        url = _url("conferences:submission", thesis_id=thesis.pk, submission_id=submission.pk)
        summary = _("Deadline: %(kind)s") % {"kind": deadline.get_kind_display()}
        lines = [
            "BEGIN:VEVENT",
            f"UID:deadline-{deadline.pk}@{_host()}",
            f"DTSTAMP:{ics.utc(now)}",
            f"DTSTART:{ics.utc(deadline.due_at)}",
            f"DTEND:{ics.utc(deadline.due_at)}",
            "SEQUENCE:0",
            "STATUS:CONFIRMED",
            "TRANSP:TRANSPARENT",
            f"SUMMARY:{ics.text(summary)}",
            f"DESCRIPTION:{ics.text(_('Details on the platform:') + ' ' + url)}",
            f"URL:{url}",
            "END:VEVENT",
        ]
        out.append((deadline.due_at, lines))
    return out


def _missions(user: Any, thesis: Any, now: dt.datetime) -> list[tuple[Any, list[str]]]:
    from apps.travel.ics import _event
    from apps.travel.models import Mission

    today = timezone.localdate(now)
    rows = Mission.objects.visible_to(user, thesis).filter(
        start_date__gte=today - BEFORE, start_date__lte=today + AFTER
    )
    out = []
    for mission in rows:
        url = _url("travel:mission", thesis_id=thesis.pk, mission_id=mission.pk)
        lines = _event(mission, url, now)  # the M7 export's event (its UID, its labels)
        for line in lines:  # a value never brings a control character
            ics.text(line)
        start = dt.datetime.combine(mission.start_date, dt.time(0), tzinfo=dt.UTC)
        out.append((start, lines))
    return out


def _objectives(user: Any, thesis: Any, now: dt.datetime) -> list[tuple[Any, list[str]]]:
    from apps.objectives.models import Objective, Status

    today = timezone.localdate(now)
    rows = Objective.objects.readable_by(user, thesis).filter(
        approved_at__isnull=False,
        status__in=(Status.PLANNED, Status.IN_PROGRESS),
        due_date__gte=today - BEFORE,
        due_date__lte=today + AFTER,
    )
    out = []
    for objective in rows:
        url = _url("objectives:detail", thesis_id=thesis.pk, objective_id=objective.pk)
        summary = _("Objective due (%(category)s)") % {"category": objective.get_category_display()}
        start = dt.datetime.combine(objective.due_date, dt.time(0), tzinfo=dt.UTC)
        out.append(
            (start, _all_day(f"objective-{objective.pk}", objective.due_date, summary, url, now))
        )
    return out


def build(feed: CalendarFeed, now: dt.datetime | None = None) -> bytes:
    """The .ics of ``feed``, in the platform's language, sorted (stable bytes)."""
    now = now or timezone.now()
    user, thesis = feed.user, feed.thesis
    with translation.override(settings.LANGUAGE_CODE):
        events: list[tuple[Any, list[str]]] = []
        for part in (_meetings, _items, _deadlines, _missions, _objectives):
            events += part(user, thesis, now)
        events.sort(key=lambda e: (e[0], e[1][1]))
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//these-platform//feed//FR",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            f"X-WR-CALNAME:{ics.text(_('CIFRE thesis'))}",
            "X-PUBLISHED-TTL:PT1H",
        ]
        for _start, event in events:
            lines += event
        lines.append("END:VCALENDAR")
    return (ics.CRLF.join(ics.fold(line) for line in lines) + ics.CRLF).encode()
