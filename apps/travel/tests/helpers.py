"""Shared fixtures of the travel tests. Fictitious data only (CLAUDE.md rule 10)."""

from __future__ import annotations

import datetime as dt
import re
import secrets
from types import SimpleNamespace
from typing import Any

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.accounts.tests.helpers import mfa_login

D = dt.date
# A Wednesday (10:00 in Paris), in CIFRE year 1 of make_team()'s thesis
# (2026-12-01 .. 2027-11-30).
NOW = dt.datetime(2027, 1, 20, 9, 0, tzinfo=dt.UTC)
TODAY = D(2027, 1, 20)

PASSWORD = secrets.token_urlsafe(16)
SITE_ORIGIN = "http://testserver"
ROLES = ("student", "director", "co_supervisor", "tutor", "csi", "guest", "outsider")
READERS = ("student", "director", "co_supervisor", "tutor")


def make_team() -> SimpleNamespace:
    thesis = ThesisFactory.create(
        cifre_start_date=D(2026, 12, 1),
        first_enrollment_date=D(2026, 12, 15),
        planned_end_date=D(2029, 11, 30),
    )

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(
            thesis=thesis, role=role, user__password=PASSWORD, **kwargs
        ).user

    return SimpleNamespace(
        thesis=thesis,
        student=member(Role.PHD_STUDENT),
        director=member(Role.DIRECTOR),
        co_supervisor=member(Role.CO_SUPERVISOR),
        tutor=member(Role.INDUSTRIAL_TUTOR),
        csi=member(Role.CSI_MEMBER, active_until=timezone.now() + dt.timedelta(days=60)),
        guest=member(Role.GUEST),
        outsider=UserFactory.create(password=PASSWORD),
    )


def client_for(team: SimpleNamespace, role: str, *, mfa: bool = False) -> Client:
    client = Client()
    if mfa:
        mfa_login(client, getattr(team, role), PASSWORD)
    else:
        client.force_login(getattr(team, role))
    return client


def browser_for(team: SimpleNamespace, role: str, *, mfa: bool = False) -> Client:
    """A client that checks CSRF like a browser (CLAUDE.md, POST tests)."""
    signed = client_for(team, role, mfa=mfa)
    client = Client(enforce_csrf_checks=True)
    client.cookies = signed.cookies
    return client


def browser_post(client: Client, page_url: str, url: str, data: dict[str, Any]) -> Any:
    page = client.get(page_url)
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode())
    assert match, "no CSRF token in the page"
    return client.post(
        url, {**data, "csrfmiddlewaretoken": match.group(1)}, HTTP_ORIGIN=SITE_ORIGIN
    )


def url(team: SimpleNamespace, name: str, **kw: Any) -> str:
    return reverse(f"travel:{name}", kwargs={"thesis_id": team.thesis.pk, **kw})


def marker() -> str:
    return "TEMOIN-" + secrets.token_hex(6)


def mission_of(team: SimpleNamespace, **overrides: Any) -> Any:
    """A fictitious mission of the student (to the LIRMM, paid by ST by default),
    without filling presence days unless asked."""
    from apps.travel import services

    fields: dict[str, Any] = {
        "thesis": team.thesis,
        "user": team.student,
        "start_date": D(2027, 2, 8),
        "end_date": D(2027, 2, 10),
        "destination": "LIRMM",
        "payer": "ST",
        "purpose": "Séjour fictif au laboratoire.",
        "fill_presence": False,
    }
    fields.update(overrides)
    return services.create_mission(**fields)


def database_dump() -> list[str]:
    """Every row of every table, as JSON text (to look for a marker in clear)."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        tables = [row[0] for row in cursor.fetchall()]
        dumps: list[str] = []
        for table in tables:
            # Names come from the catalogue itself, quoted: no user input here.
            cursor.execute(
                'SELECT row_to_json(t)::text FROM "' + table.replace('"', '""') + '" t'  # noqa: S608  # nosec B608
            )
            dumps += [row[0] for row in cursor.fetchall()]
    return dumps
