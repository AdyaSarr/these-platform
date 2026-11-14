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

from .models import Meeting

CRLF = "\r\n"
MAX_OCTETS = 75


def escape(text: str) -> str:
    """TEXT value (RFC 5545 §3.3.11): backslash, semicolon, comma, newline."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Fold at 75 octets (RFC 5545 §3.1), never inside a UTF-8 sequence."""
    out: list[str] = []
    current, size, limit = "", 0, MAX_OCTETS
    for char in line:
        width = len(char.encode())
        if size + width > limit:
            out.append(current)
            current, size, limit = "", 0, MAX_OCTETS - 1  # the leading space counts
        current += char
        size += width
    out.append(current)
    return (CRLF + " ").join(out)


def _utc(instant: dt.datetime) -> str:
    return instant.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


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
