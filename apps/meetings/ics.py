"""The .ics file of one meeting (RFC 5545), written here: its content is fully
ours, so no parser or library is involved.

It leaves the platform (Outlook, ST's Exchange): it carries the type and the
place as labels, the times in UTC, the SEQUENCE and the status, and a link to
the meeting. Never a sealed text (title, agenda, minutes, place detail, video
link), never an attendee.
"""

from __future__ import annotations

import datetime as dt
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext as _

from apps.core.ics import CRLF as CRLF
from apps.core.ics import MAX_OCTETS as MAX_OCTETS
from apps.core.ics import escape as escape
from apps.core.ics import fold as fold
from apps.core.ics import utc as _utc

from .models import Meeting


def summary(meeting: Meeting) -> str:
    """The type's label (and the presentation venue): never the title."""
    label = str(meeting.get_kind_display())
    if meeting.kind == Meeting.Kind.PRESENTATION and meeting.presentation_venue:
        label = f"{label} — {meeting.get_presentation_venue_display()}"
    return label


def calendar(meeting: Meeting, *, url: str, now: dt.datetime) -> bytes:
    with translation.override(settings.LANGUAGE_CODE):
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//these-platform//meetings//FR",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "BEGIN:VEVENT",
            f"UID:{meeting.pk}@{urlsplit(settings.SITE_URL).hostname or 'localhost'}",
            f"DTSTAMP:{_utc(now)}",
            f"DTSTART:{_utc(meeting.starts_at)}",
            f"DTEND:{_utc(meeting.ends_at)}",
            f"SEQUENCE:{meeting.ics_sequence}",
            f"STATUS:{'CANCELLED' if meeting.is_cancelled else 'CONFIRMED'}",
            f"SUMMARY:{escape(summary(meeting))}",
            f"LOCATION:{escape(str(meeting.get_location_display()))}",
            f"DESCRIPTION:{escape(_('Details on the platform:') + ' ' + url)}",
            f"URL:{url}",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    return (CRLF.join(fold(line) for line in lines) + CRLF).encode()
