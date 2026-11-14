"""Shared fixtures of the conferences tests. Fictitious data only (CLAUDE.md rule 10):
invented venue names, example.org addresses."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.conferences import services
from apps.conferences.models import Deadline, Edition, Submission, Venue
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.objectives.tests.helpers import make_team, marker
from apps.vault import services as vault
from apps.vault.models import DocumentVersion

__all__ = ["NOW", "D", "L", "T", "make_team", "marker"]

D, T = dt.date, dt.time
# Inside the bounds of make_team()'s thesis (CIFRE start 2026-12-01).
NOW = dt.datetime(2027, 1, 10, 9, 0, tzinfo=dt.UTC)
PAPER_DAY = dt.date(2027, 3, 1)
END_OF_DAY = dt.time(23, 59)


def venue_of(team: SimpleNamespace, **kw: Any) -> Venue:
    fields: dict[str, Any] = {
        "thesis": team.thesis,
        "user": team.student,
        "kind": "CONFERENCE",
        "acronym": "SFCA",
        "name": "Symposium fictif sur les canaux auxiliaires",
        "url": "https://sfca.example.org/",
    }
    fields.update(kw)
    return services.create_venue(**fields)


def edition_of(team: SimpleNamespace, venue: Venue | None = None, **kw: Any) -> Edition:
    fields: dict[str, Any] = {
        "venue": venue or venue_of(team),
        "user": team.student,
        "year": 2027,
        "location": "Ville fictive",
        "event_start": D(2027, 9, 6),
        "event_end": D(2027, 9, 9),
    }
    fields.update(kw)
    return services.create_edition(**fields)


def deadline_of(
    team: SimpleNamespace,
    edition: Edition,
    kind: str = "PAPER",
    day: dt.date = PAPER_DAY,
    wall: dt.time = END_OF_DAY,
    tz_name: str = "AoE",
) -> Deadline:
    return services.add_deadline(
        edition=edition, user=team.student, kind=kind, day=day, wall=wall, tz_name=tz_name
    )


def submission_of(team: SimpleNamespace, edition: Edition | None = None, **kw: Any) -> Submission:
    edition = edition or edition_of(team)
    fields: dict[str, Any] = {
        "thesis": team.thesis,
        "user": team.student,
        "venue": edition.venue,
        "edition": edition,
        "title": "Attaque fictive sur un circuit fictif",
        "abstract": "Résumé fictif.",
        "level": L.CONFIDENTIAL,
    }
    fields.update(kw)
    return services.create_submission(**fields)


def article_version(
    team: SimpleNamespace, submission: Submission, *, document: Any = None
) -> DocumentVersion:
    """A new version of the article (a new document when ``document`` is None),
    linked to the submission."""
    upload = SimpleUploadedFile("article.pdf", b"%PDF-1.7\n%article fictif\n%%EOF\n")
    if document is None:
        document = vault.store(
            thesis=team.thesis, upload=upload, title="Article fictif", level=L.CONFIDENTIAL,
            user=team.student,
        )  # fmt: skip
        services.link_document(submission=submission, user=team.student, document=document)
        return DocumentVersion.objects.get(document=document, version=1)
    version = vault.add_version(document=document, upload=upload, user=team.student)
    return DocumentVersion.objects.get(pk=version.pk)


def granted(
    team: SimpleNamespace, submission: Submission, version: DocumentVersion, **kw: Any
) -> Any:
    approval = services.request_approval(
        submission=submission, user=team.student, version=version, **kw
    )
    return services.decide_approval(
        approval=approval, user=team.tutor, decision="GRANTED", recent_mfa=True
    )
