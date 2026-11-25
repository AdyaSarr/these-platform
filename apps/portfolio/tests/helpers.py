"""Shared fixtures of the portfolio tests (M9a, M9b). Fictitious data only (CLAUDE.md
rule 10). The team is the one of the activity tests (the seven roles); every
training, teaching, outreach action, skill and submission is made through the
services, by the student."""

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
    "details",
    "make_team",
    "mfa_browser",
    "outreach",
    "publication",
    "skill",
    "teaching",
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
# M9b: teaching and outreach are read by the whole supervision, never by the CSI.
ACTIVITY_READERS = ("student", "director", "co_supervisor", "tutor", "st_co_supervisor")
ACTIVITY_NOT_READERS = ("csi", "guest", "outsider")
# M9b: the publication details follow the submissions (SUBMISSION_READ).
SUBMISSION_READERS = ACTIVITY_READERS


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


def publication(
    team: SimpleNamespace,
    level: L = L.PUBLIC,
    *,
    title: str = "Article publié fictif",
    status: str = "PUBLISHED",
    venue: Any = None,
    presentation_kind: str = "PAPER",
    external_authors: str = "",
) -> Any:
    """A submission of ``level`` (not ST work: no approval needed) moved to ``status``
    (PREPARING, SUBMITTED, ACCEPTED or PUBLISHED); at the SFCA 2027 by default, or at
    ``venue`` without an edition."""
    from apps.conferences import services as conferences
    from apps.conferences.tests.helpers import submission_of

    target: dict[str, Any] = {} if venue is None else {"venue": venue, "edition": None}
    submission = submission_of(
        team, level=level, title=title, st_work=False, presentation_kind=presentation_kind,
        external_authors=external_authors, **target,
    )  # fmt: skip
    steps = ("SUBMITTED", "ACCEPTED", "PUBLISHED")
    for step in steps[: steps.index(status) + 1] if status in steps else ():
        submission = conferences.set_status(submission=submission, user=team.student, status=step)
    return submission


def details(team: SimpleNamespace, submission: Any, **values: Any) -> Any:
    from apps.portfolio import services

    fields: dict[str, Any] = {"adum_category": "CONFERENCE_PAPER", "presented_on": None,
                              "doi": "", "hal_id": "", "volume": "", "pages": "",
                              "bibtex_key": ""}  # fmt: skip
    fields.update(values)
    return services.set_publication_details(submission=submission, user=team.student, values=fields)


def teaching(
    team: SimpleNamespace,
    level: L = L.PUBLIC,
    *,
    course: str = "Cours fictif",
    institution: str = "Université fictive",
    notes: str = "",
    kind: str = "TD",
    hours: str = "12",
    start: dt.date = START,
    end: dt.date | None = None,
) -> Any:
    from apps.portfolio import services

    return services.create_teaching(
        thesis=team.thesis, user=team.student, course=course, institution=institution,
        notes=notes, kind=kind, hours=hours, start_date=start, end_date=end or start,
        level=level,
    )  # fmt: skip


def outreach(
    team: SimpleNamespace,
    level: L = L.PUBLIC,
    *,
    title: str = "Action fictive",
    audience: str = "Lycée fictif",
    notes: str = "",
    kind: str = "SCHOOL",
    date: dt.date = START,
) -> Any:
    from apps.portfolio import services

    return services.create_outreach(
        thesis=team.thesis, user=team.student, title=title, audience=audience, notes=notes,
        kind=kind, date=date, level=level,
    )  # fmt: skip


def skill(
    team: SimpleNamespace,
    *,
    title: str = "Compétence fictive",
    evidence: str = "",
    category: str = "TECHNICAL",
) -> Any:
    from apps.portfolio import services

    return services.create_skill(
        thesis=team.thesis, user=team.student, title=title, evidence=evidence, category=category
    )
