"""Shared fixtures of the meetings tests. Fictitious data only (CLAUDE.md rule 10)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from apps.core.confidentiality import ConfidentialityLevel as L
from apps.meetings import services
from apps.meetings.models import Meeting
from apps.objectives.tests.helpers import make_team, marker

__all__ = ["START", "L", "make_team", "marker", "meeting_of"]

# 2027-01-12 14:00 in Paris (winter time): inside the bounds of make_team()'s thesis.
START = dt.datetime(2027, 1, 12, 13, 0, tzinfo=dt.UTC)


def meeting_of(team: SimpleNamespace, **overrides: Any) -> Meeting:
    fields: dict[str, Any] = {
        "thesis": team.thesis,
        "user": team.student,
        "kind": Meeting.Kind.SUPERVISION,
        "starts_at": START,
        "duration_minutes": 60,
        "location": Meeting.Location.VISIO,
        "level": L.PUBLIC,
        "title": "Point fictif d'avancement",
        "attendees": [team.student, team.director, team.co_supervisor],
    }
    fields.update(overrides)
    return services.create_meeting(**fields)
