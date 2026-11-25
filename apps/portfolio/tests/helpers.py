"""Shared fixtures of the portfolio tests (M9a). Fictitious data only (CLAUDE.md
rule 10). The team is the one of the activity tests (the seven roles); every
training is made through the service, by the student."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from django.urls import reverse

from apps.activity.tests.helpers import (
    NOW,
    PASSWORD,
    ROLE_ATTRS,
    SITE_ORIGIN,
    browser_for,
    browser_post,
    client_for,
    make_team,
)
from apps.core.confidentiality import ConfidentialityLevel as L

__all__ = [
    "NOW",
    "PASSWORD",
    "ROLE_ATTRS",
    "SITE_ORIGIN",
    "TODAY",
    "D",
    "L",
    "browser_for",
    "browser_post",
    "client_for",
    "make_team",
    "mfa_browser",
    "training",
    "url",
]

D = dt.date
TODAY = D(2027, 1, 20)  # NOW in Paris
START = D(2027, 1, 11)  # a finished training by default
# Who reads the trainings, and who does not (docs/modules/M09-portfolio.md).
READERS = ("student", "director", "co_supervisor", "tutor", "csi")
NOT_READERS = ("st_co_supervisor", "guest", "outsider")
NOT_STUDENT = ("director", "co_supervisor", "tutor", "st_co_supervisor", "csi", "guest",
               "outsider")  # fmt: skip


def url(team: SimpleNamespace, name: str, **kw: Any) -> str:
    return reverse(f"portfolio:{name}", kwargs={"thesis_id": team.thesis.pk, **kw})


def training(
    team: SimpleNamespace,
    level: L = L.PUBLIC,
    *,
    title: str = "Formation fictive",
    provider: str = "Organisme fictif",
    notes: str = "",
    category: str = "SCIENTIFIC",
    hours: str = "6",
    start: dt.date = START,
    end: dt.date | None = None,
    certificate: Any = None,
) -> Any:
    from apps.portfolio import services

    return services.create_training(
        thesis=team.thesis, user=team.student, title=title, provider=provider, notes=notes,
        category=category, hours=hours, start_date=start, end_date=end or start,
        certificate=certificate, level=level,
    )  # fmt: skip


def mfa_browser(user: Any) -> Any:
    from apps.lifecycle.tests.helpers import mfa_browser as browser

    return browser(user)
