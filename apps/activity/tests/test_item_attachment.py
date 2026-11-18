"""One line for a piece of the administrative file and its attachment (M13a review):
the event of the piece (« Pièce déposée ») covers the document it carries; no
« Nouveau document » nor « Nouvelle version » beside it, in the feed or by e-mail."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail

from apps.accounts.roles import Role
from apps.activity.models import ActivityNotification, Event
from apps.activity.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return h.make_team()


def _piece(team: SimpleNamespace) -> Any:
    from apps.admin_file.tests.factories import RequiredItemFactory
    from apps.core.confidentiality import ConfidentialityLevel as L

    return RequiredItemFactory.create(
        thesis=team.thesis, code="custom-piece-jointe", label="Pièce jointe fictive",
        section="ACADEMIC", validator_roles=[Role.DIRECTOR], default_level=L.INTERNAL_LIRMM,
        attachment_required=True,
    )  # fmt: skip


def _kinds(team: SimpleNamespace) -> list[str]:
    return list(
        Event.objects.filter(thesis=team.thesis)
        .order_by("created_at")
        .values_list("kind", flat=True)
    )


def test_a_piece_filed_with_its_file_makes_one_line(team: SimpleNamespace) -> None:
    from apps.activity.preferences import set_preference
    from apps.admin_file import services

    set_preference(team.director, team.thesis, "INFO", "IMMEDIATE")
    mail.outbox.clear()
    services.submit(item=_piece(team), user=team.student, upload=h.pdf())
    assert _kinds(team) == ["ITEM_SUBMITTED"]
    feed = h.client_for(team.director).get(h.url(team, "feed")).content.decode()
    main = feed.split("<main", 1)[1]
    assert main.count('class="activity-item"') == 1 and "Pièce déposée" in main
    assert not ActivityNotification.objects.filter(event__kind="DOCUMENT_ADDED").exists()


def test_a_new_version_of_the_attachment_makes_no_line_either(team: SimpleNamespace) -> None:
    from apps.admin_file import services

    piece = services.submit(item=_piece(team), user=team.student, upload=h.pdf())
    piece = services.reject(item=piece, user=team.director, reason="ILLEGIBLE")
    services.submit(item=piece, user=team.student, upload=h.pdf("version 2"))
    assert "DOCUMENT_VERSION_ADDED" not in _kinds(team)
    assert "DOCUMENT_ADDED" not in _kinds(team)


def test_the_attached_document_is_still_a_document(team: SimpleNamespace) -> None:
    """Only the line goes: the document itself is stored, journaled and listed."""
    from apps.admin_file import services
    from apps.vault.models import AuditEvent, Document

    piece = services.submit(item=_piece(team), user=team.student, upload=h.pdf())
    assert Document.objects.filter(pk=piece.document_id).exists()
    assert AuditEvent.objects.filter(action="DOCUMENT_CREATED").exists()


def test_a_document_stored_by_itself_still_has_its_line(team: SimpleNamespace) -> None:
    from apps.core.confidentiality import ConfidentialityLevel as L

    h.document(team, L.PUBLIC)
    assert _kinds(team) == ["DOCUMENT_ADDED"]
