"""Shared fixtures of the activity tests (M11a). Fictitious data only (CLAUDE.md
rule 10).

Every object is made through its module's service, by the student: the event is
written by the service itself, as in production. The team holds the seven roles.
"""

from __future__ import annotations

import datetime as dt
import secrets
from types import SimpleNamespace
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.core.confidentiality import ConfidentialityLevel as L

D = dt.date
# A Wednesday, 10:00 in Paris, in CIFRE year 1 of make_team()'s thesis.
NOW = dt.datetime(2027, 1, 20, 9, 0, tzinfo=dt.UTC)
PASSWORD = secrets.token_urlsafe(16)
SITE_ORIGIN = "http://testserver"

ROLE_ATTRS = {
    Role.PHD_STUDENT: "student",
    Role.DIRECTOR: "director",
    Role.CO_SUPERVISOR: "co_supervisor",
    Role.INDUSTRIAL_TUTOR: "tutor",
    Role.INDUSTRIAL_CO_SUPERVISOR: "st_co_supervisor",
    Role.CSI_MEMBER: "csi",
    Role.GUEST: "guest",
}


def make_team() -> SimpleNamespace:
    from apps.objectives.services import sync_years

    thesis = ThesisFactory.create(
        cifre_start_date=D(2026, 12, 1),
        first_enrollment_date=D(2026, 12, 15),
        planned_end_date=D(2029, 11, 30),
    )

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(
            thesis=thesis, role=role, user__password=PASSWORD, **kwargs
        ).user

    team = SimpleNamespace(thesis=thesis, outsider=UserFactory.create(password=PASSWORD))
    for role, attr in ROLE_ATTRS.items():
        extra: dict[str, Any] = {}
        if role is Role.CSI_MEMBER:
            extra["active_until"] = timezone.now() + dt.timedelta(days=60)
        setattr(team, attr, member(role, **extra))
    sync_years(thesis)
    return team


def client_for(user: Any) -> Client:
    client = Client()
    client.force_login(user)
    return client


def browser_for(user: Any) -> Client:
    """A client that checks CSRF like a browser (CLAUDE.md: HTTP_ORIGIN, token)."""
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    return client


def browser_post(client: Client, page_url: str, url: str, data: dict[str, Any]) -> Any:
    import re

    page = client.get(page_url).content.decode()
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)
    assert match, "no CSRF token in the page"
    return client.post(
        url, {**data, "csrfmiddlewaretoken": match.group(1)}, HTTP_ORIGIN=SITE_ORIGIN
    )


def url(team: SimpleNamespace, name: str, **kw: Any) -> str:
    return reverse(f"activity:{name}", kwargs={"thesis_id": team.thesis.pk, **kw})


def pdf(text: str = "piece fictive") -> SimpleUploadedFile:
    return SimpleUploadedFile("piece.pdf", f"%PDF-1.7\n%{text}\n%%EOF\n".encode())


# --------------------------------------------------------------------------- objects


def document(team: SimpleNamespace, level: L, title: str = "Note fictive") -> Any:
    from apps.vault import services as vault

    return vault.store(
        thesis=team.thesis, upload=pdf(), title=title, level=level, user=team.student
    )


def meeting(team: SimpleNamespace, level: L, *, days: int = 7, attendees: Any = None) -> Any:
    from apps.meetings import services as meetings
    from apps.meetings.models import Meeting

    return meetings.create_meeting(
        thesis=team.thesis,
        user=team.student,
        kind=Meeting.Kind.SUPERVISION,
        starts_at=NOW + dt.timedelta(days=days),
        duration_minutes=60,
        location=Meeting.Location.VISIO,
        level=level,
        title="TEMOIN-FUITE-TITRE-REUNION",
        attendees=attendees if attendees is not None else [team.student],
    )


def submission(team: SimpleNamespace, level: L) -> Any:
    from apps.conferences.tests.helpers import submission_of

    return submission_of(team, level=level, title="TEMOIN-FUITE-TITRE-ARTICLE")


def objective(team: SimpleNamespace, level: L) -> Any:
    from apps.objectives import services as objectives
    from apps.objectives.models import Category, ThesisYear

    return objectives.create_objective(
        thesis=team.thesis,
        user=team.student,
        year=ThesisYear.objects.get(thesis=team.thesis, number=1),
        title="TEMOIN-FUITE-TITRE-OBJECTIF",
        category=Category.RESEARCH,
        due_date=D(2027, 6, 30),
        level=level,
    )


def mission(team: SimpleNamespace, destination: str, payer: str, *, start: dt.date) -> Any:
    from apps.travel import services as travel

    return travel.create_mission(
        thesis=team.thesis,
        user=team.student,
        start_date=start,
        end_date=start,
        destination=destination,
        payer=payer,
        purpose="TEMOIN-FUITE-MOTIF-MISSION",
        fill_presence=False,
    )


def item(team: SimpleNamespace, section: str, validator_roles: list[Role]) -> Any:
    """A fictitious item (no attachment required), submitted by the student."""
    from apps.admin_file import services as admin_file
    from apps.admin_file.tests.factories import RequiredItemFactory

    piece = RequiredItemFactory.create(
        thesis=team.thesis,
        section=section,
        validator_roles=validator_roles,
        attachment_required=False,
    )
    return admin_file.submit(item=piece, user=team.student)


def events_of(kind: str) -> list[Any]:
    from apps.activity.models import Event

    return list(Event.objects.filter(kind=kind).order_by("created_at"))
