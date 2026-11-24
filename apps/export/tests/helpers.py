"""Shared fixtures of the export tests (M13b). Fictitious data only (rule 10).

``world(team)`` builds, through the modules' services, one object of each kind the
export reads, at PUBLIC, CONFIDENTIAL and INTERNAL_ST, and the secrets the export
must never contain. Each secret is a witness (``TEMOIN-FUITE-…`` or a value
drawn by the code itself) listed in ``world.forbidden`` / ``world.st_witnesses``.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import zipfile
from types import SimpleNamespace
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.activity.tests.helpers import (
    NOW,
    PASSWORD,
    ROLE_ATTRS,
    SITE_ORIGIN,
    D,
    client_for,
    make_team,
)
from apps.core.confidentiality import ConfidentialityLevel as L

__all__ = [
    "NOW",
    "PASSWORD",
    "ROLE_ATTRS",
    "SITE_ORIGIN",
    "D",
    "L",
    "client_for",
    "csrf_post",
    "make_team",
    "mfa_browser",
    "pdf",
    "read",
    "run",
    "url",
    "world",
]  # fmt: skip

ST_WITNESSES = (
    "TEMOIN-FUITE-DOC-ST",
    "TEMOIN-FUITE-CONTENU-ST",
    "TEMOIN-FUITE-REUNION-ST",
    "TEMOIN-FUITE-MOTIF-ST",
)


def pdf(text: str) -> SimpleUploadedFile:
    return SimpleUploadedFile("piece.pdf", f"%PDF-1.7\n%{text}\n%%EOF\n".encode())


def pdf_bytes(text: str) -> bytes:
    return f"%PDF-1.7\n%{text}\n%%EOF\n".encode()


def url(team: SimpleNamespace, name: str, **kw: Any) -> str:
    return reverse(f"export:{name}", kwargs={"thesis_id": team.thesis.pk, **kw})


def world(team: SimpleNamespace) -> SimpleNamespace:
    from allauth.mfa.models import Authenticator
    from django.test import RequestFactory

    from apps.accounts import services as accounts
    from apps.accounts.models import Membership
    from apps.meetings import services as meetings
    from apps.meetings.models import Meeting
    from apps.objectives import services as objectives
    from apps.objectives.models import Category, ThesisYear
    from apps.travel import services as travel
    from apps.vault import services as vault
    from apps.vault.audit import AuditAction, audit

    s, w = team.student, SimpleNamespace()
    # Documents: two versions, a CONFIDENTIAL one, an INTERNAL_ST one.
    w.doc = vault.store(thesis=team.thesis, upload=pdf("contenu-public-v1"),
                        title="Rapport annuel", level=L.PUBLIC, user=s)  # fmt: skip
    vault.add_version(document=w.doc, upload=pdf("contenu-public-v2"), user=s)
    w.conf = vault.store(thesis=team.thesis, upload=pdf("contenu-confidentiel"),
                         title="Contrat fictif", level=L.CONFIDENTIAL, user=s)  # fmt: skip
    w.st_doc = vault.store(thesis=team.thesis, upload=pdf("TEMOIN-FUITE-CONTENU-ST"),
                           title="TEMOIN-FUITE-DOC-ST", level=L.INTERNAL_ST, user=s)  # fmt: skip
    # An objective and a comment of the director (a supervisor's comment).
    w.objective = objectives.create_objective(
        thesis=team.thesis, user=s, year=ThesisYear.objects.get(thesis=team.thesis, number=1),
        title="Objectif fictif", category=Category.RESEARCH, due_date=D(2027, 6, 30),
        level=L.PUBLIC, description="Description fictive",
    )  # fmt: skip
    w.comment = objectives.add_comment(
        objective=w.objective, user=team.director, body="Commentaire du directeur fictif"
    )
    # Meetings: one PUBLIC (with the director), one INTERNAL_ST (with the tutor).
    common = {"thesis": team.thesis, "user": s, "kind": Meeting.Kind.SUPERVISION,
              "duration_minutes": 60, "location": Meeting.Location.VISIO}  # fmt: skip
    w.meeting = meetings.create_meeting(**common, starts_at=NOW + dt.timedelta(days=3),
                                        level=L.PUBLIC, title="Point d'avancement fictif",
                                        attendees=[s, team.director])  # fmt: skip
    w.st_meeting = meetings.create_meeting(**common, starts_at=NOW + dt.timedelta(days=4),
                                           level=L.INTERNAL_ST, title="TEMOIN-FUITE-REUNION-ST",
                                           attendees=[s, team.tutor])  # fmt: skip
    # Missions: to the LIRMM (paid by the LIRMM), to Rousset (paid by ST: INTERNAL_ST).
    w.mission = travel.create_mission(
        thesis=team.thesis, user=s, start_date=D(2027, 2, 2), end_date=D(2027, 2, 2),
        destination="LIRMM", payer="LIRMM", purpose="Motif fictif", fill_presence=False,
    )  # fmt: skip
    w.st_mission = travel.create_mission(
        thesis=team.thesis, user=s, start_date=D(2027, 2, 3), end_date=D(2027, 2, 3),
        destination="ROUSSET", payer="ST", purpose="TEMOIN-FUITE-MOTIF-ST", fill_presence=False,
    )  # fmt: skip
    # Secrets the export must never hold.
    mfa_witness = {"secret": "TEMOIN-FUITE-SECRET-MFA"}  # pragma: allowlist secret (a witness)
    Authenticator.objects.create(user=s, type="totp", data=mfa_witness)
    from apps.feeds import services as feeds

    w.feed, w.feed_token = feeds.create(user=s, thesis=team.thesis, recent_mfa=True)
    membership = Membership.objects.active().filter(thesis=team.thesis, user=s).first()
    assert membership is not None
    w.invitation = accounts.create_invitation(
        thesis=team.thesis, email="invitee.fictive@example.org", role="CO_SUPERVISOR",
        organization=membership.organization, by=s,
    )  # fmt: skip
    audit(AuditAction.THESIS_ACCESS_DENIED, actor=team.director, thesis=team.thesis,
          reason="TEMOIN-FUITE-AUDIT-AUTRUI")  # fmt: skip
    request = RequestFactory().get("/", REMOTE_ADDR="198.51.100.23")
    w.failed = audit(AuditAction.LOGIN_FAILED, actor=s, request=request,
                     subject_email="sujet.fictif@example.org")  # fmt: skip
    w.feed.refresh_from_db()
    w.invitation.refresh_from_db()
    others = [getattr(team, a) for a in ROLE_ATTRS.values() if a != "student"]
    w.forbidden = [
        "TEMOIN-FUITE-SECRET-MFA",
        "TEMOIN-FUITE-AUDIT-AUTRUI",
        w.feed_token,
        w.feed.token_hash,
        w.invitation.token_hash,
        "invitee.fictive@example.org",
        w.failed.ip_hash,
        w.failed.details_json["email_hmac"],
        *[u.password for u in [s, *others]],
        *[u.email for u in others],
    ]
    assert all(w.forbidden)
    w.st_witnesses = ST_WITNESSES
    return w


def run(team: SimpleNamespace, **kwargs: Any) -> zipfile.ZipFile:
    """The student's export, read back as a ZIP (recent second factor)."""
    from apps.export import services as export

    started = export.start_export(thesis=team.thesis, user=team.student, recent_mfa=True,
                                  **kwargs)  # fmt: skip
    data = b"".join(started.chunks())
    return zipfile.ZipFile(io.BytesIO(data))


def read(archive: zipfile.ZipFile, name: str) -> Any:
    return json.loads(archive.read(name))


def everything(archive: zipfile.ZipFile) -> str:
    """Names and contents of every entry, as one text, to search for witnesses."""
    parts = []
    for info in archive.infolist():
        parts.append(info.filename)
        parts.append(archive.read(info).decode("utf-8", errors="replace"))
    return "\n".join(parts)


def mfa_browser(user: Any) -> Any:
    from apps.lifecycle.tests.helpers import mfa_browser as browser

    return browser(user)


def csrf_post(client: Any, team: SimpleNamespace, path: str, data: dict[str, Any]) -> Any:
    """A POST like a browser's: CSRF token from a page every member reads."""
    import re

    page = client.get(reverse("lifecycle:page", kwargs={"thesis_id": team.thesis.pk}))
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode())
    assert match, "no CSRF token in the page"
    return client.post(path, {**data, "csrfmiddlewaretoken": match.group(1)},
                       HTTP_ORIGIN=SITE_ORIGIN)  # fmt: skip
