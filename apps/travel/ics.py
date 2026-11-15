"""The .ics of missions (RFC 5545), written here like M5a's (meetings.ics): its content
is fully ours, no parser or library is involved.

It leaves the platform (Outlook, ST's Exchange): all-day events, the destination's
LABEL, a status, the SEQUENCE and a link. Never a sealed text (purpose, another
place, lodging note), never an amount, never a conference's name or place, never
the lodging.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext as _
from django.utils.translation import pgettext

from apps.meetings.ics import CRLF, escape, fold

from .choices import Destination, MissionStatus
from .models import Mission

CONFIRMED = (MissionStatus.ORDER_GRANTED, MissionStatus.DONE, MissionStatus.REIMBURSED)


def summary(mission: Mission) -> str:
    if mission.destination in (Destination.LIRMM, Destination.ROUSSET):
        return f"{_('Mission')} — {Destination(mission.destination).label}"
    if mission.destination == Destination.CONFERENCE:
        return str(Destination.CONFERENCE.label)
    return pgettext("ics", "Trip")


def _status(mission: Mission) -> str:
    if mission.status == MissionStatus.CANCELLED:
        return "CANCELLED"
    return "CONFIRMED" if mission.status in CONFIRMED else "TENTATIVE"


def _utc(instant: dt.datetime) -> str:
    return instant.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _event(mission: Mission, url: str, now: dt.datetime) -> list[str]:
    host = urlsplit(settings.SITE_URL).hostname or "localhost"
    end = mission.end_date + dt.timedelta(days=1)  # DTEND of a DATE is exclusive
    return [
        "BEGIN:VEVENT",
        f"UID:mission-{mission.pk}@{host}",
        f"DTSTAMP:{_utc(now)}",
        f"DTSTART;VALUE=DATE:{mission.start_date:%Y%m%d}",
        f"DTEND;VALUE=DATE:{end:%Y%m%d}",
        f"SEQUENCE:{mission.ics_sequence}",
        f"STATUS:{_status(mission)}",
        "TRANSP:TRANSPARENT",
        f"SUMMARY:{escape(summary(mission))}",
        f"DESCRIPTION:{escape(_('Details on the platform:') + ' ' + url)}",
        f"URL:{url}",
        "END:VEVENT",
    ]


def calendar(missions: Iterable[tuple[Mission, str]], *, now: dt.datetime) -> bytes:
    """``missions``: (mission, absolute URL of its page)."""
    with translation.override(settings.LANGUAGE_CODE):
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//these-platform//travel//FR",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
        ]
        for mission, url in missions:
            lines += _event(mission, url, now)
        lines.append("END:VCALENDAR")
    return (CRLF.join(fold(line) for line in lines) + CRLF).encode()
