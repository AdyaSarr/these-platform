"""Shared fixtures of the end-of-thesis tests (M13a). Fictitious data only."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from django.test import Client
from django.urls import reverse

from apps.activity.tests.helpers import (
    PASSWORD as PASSWORD,
)
from apps.activity.tests.helpers import (
    ROLE_ATTRS as ROLE_ATTRS,
)
from apps.activity.tests.helpers import (
    SITE_ORIGIN as SITE_ORIGIN,
)
from apps.activity.tests.helpers import (
    client_for as client_for,
)
from apps.activity.tests.helpers import (
    make_team as make_team,
)

D = dt.date
# Ten days after a defence on 2027-06-01; the closure is possible from 2027-09-01.
NOW = dt.datetime(2027, 6, 10, 9, 0, tzinfo=dt.UTC)
DEFENSE = D(2027, 6, 1)
CLOSABLE = dt.datetime(2027, 9, 2, 9, 0, tzinfo=dt.UTC)
ROLES = tuple(ROLE_ATTRS.values())  # student, director, co_supervisor, tutor, ...


def url(team: SimpleNamespace, name: str, **kw: Any) -> str:
    return reverse(f"lifecycle:{name}", kwargs={"thesis_id": team.thesis.pk, **kw})


def defended(team: SimpleNamespace, day: dt.date = DEFENSE) -> Any:
    from apps.lifecycle import services

    return services.declare_defense(
        thesis=team.thesis, user=team.director, defended_on=day, recent_mfa=True
    )


def requested(team: SimpleNamespace, by: Any = None, reason: str = "defended") -> Any:
    from apps.lifecycle import services

    return services.request_closure(
        thesis=team.thesis, user=by or team.director, reason=reason, recent_mfa=True
    )


def closed(team: SimpleNamespace, time_machine: Any) -> Any:
    """Defended on DEFENSE, closure requested by the director, confirmed by the
    student at CLOSABLE."""
    from apps.lifecycle import services

    defended(team)
    time_machine.move_to(CLOSABLE, tick=True)
    request = requested(team)
    return services.confirm_closure(request=request, user=team.student, recent_mfa=True)


def mfa_browser(user: Any) -> Client:
    """Signed in with a recent second factor, then a client that checks CSRF."""
    from apps.accounts.tests.helpers import mfa_login

    signed = Client()
    mfa_login(signed, user, PASSWORD)
    client = Client(enforce_csrf_checks=True)
    client.cookies = signed.cookies
    return client


def browser_post(client: Client, page_url: str, url: str, data: dict[str, Any]) -> Any:
    import re

    page = client.get(page_url).content.decode()
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)
    assert match, "no CSRF token in the page"
    return client.post(
        url, {**data, "csrfmiddlewaretoken": match.group(1)}, HTTP_ORIGIN=SITE_ORIGIN
    )


def status_of(team: SimpleNamespace) -> str:
    from apps.accounts.models import Thesis

    return str(Thesis.objects.get(pk=team.thesis.pk).status)


def audited(action: str) -> list[dict[str, Any]]:
    from apps.vault.models import AuditEvent

    return [e.details_json for e in AuditEvent.objects.filter(action=action).order_by("seq")]
