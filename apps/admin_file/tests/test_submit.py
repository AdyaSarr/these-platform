"""Submission with an attachment: only through vault.services, at an allowed level,
never with a document of another thesis or already attached elsewhere; the
version approved is the version submitted.

transaction=True (CLAUDE.md): the vault stores after audited operations.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction

from apps.accounts.roles import Role
from apps.admin_file import services
from apps.admin_file.models import ItemStatus, Reason, RequiredItem, Section
from apps.admin_file.tests.factories import RequiredItemFactory
from apps.admin_file.tests.helpers import make_team
from apps.admin_file.transitions import StaleSubmissionError
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault import services as vault
from apps.vault.models import AuditEvent, Document

pytestmark = pytest.mark.django_db(transaction=True)

S = ItemStatus
D, T = Role.DIRECTOR, Role.INDUSTRIAL_TUTOR


def _pdf(marker: str = "fictive") -> SimpleUploadedFile:
    return SimpleUploadedFile("piece.pdf", f"%PDF-1.7\n%Piece {marker}\n%%EOF\n".encode())


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _item(team: SimpleNamespace, **kwargs: Any) -> RequiredItem:
    kwargs.setdefault("section", Section.ACADEMIC)
    kwargs.setdefault("validator_roles", [D])
    kwargs.setdefault("default_level", L.INTERNAL_LIRMM)
    kwargs.setdefault("attachment_required", True)
    return RequiredItemFactory.create(thesis=team.thesis, **kwargs)


def _store(team: SimpleNamespace, level: L = L.INTERNAL_LIRMM, **kw: Any) -> Document:
    thesis = kw.pop("thesis", team.thesis)
    user = kw.pop("user", team.student)
    return vault.store(thesis=thesis, upload=_pdf(), title="Pièce fictive", level=level, user=user)


def test_upload_is_stored_by_the_vault_at_the_default_level(team: SimpleNamespace) -> None:
    item = _item(team, default_level=L.INTERNAL_LIRMM)
    services.submit(item=item, user=team.student, upload=_pdf())
    item.refresh_from_db()
    assert item.status == S.SUBMITTED
    assert item.document is not None
    assert item.document.thesis_id == team.thesis.pk
    assert item.document.confidentiality == L.INTERNAL_LIRMM
    assert item.submitted_version == 1
    assert AuditEvent.objects.filter(action="DOCUMENT_CREATED", object_id=str(item.document.pk))
    event = AuditEvent.objects.get(action="ADMIN_ITEM_SUBMITTED")
    assert event.details_json == {"from": "TODO", "to": "SUBMITTED", "cycle": 1, "version": 1}


def test_a_chosen_level_must_be_readable_by_every_validator(team: SimpleNamespace) -> None:
    item = _item(team, section=Section.CIFRE, validator_roles=[D, T], default_level=L.CONFIDENTIAL)
    assert services.allowed_levels(item) == [L.PUBLIC, L.CONFIDENTIAL]
    for level in (L.INTERNAL_LIRMM, L.INTERNAL_ST):  # one validator could not read it
        with pytest.raises(ValidationError):
            services.submit(item=item, user=team.student, upload=_pdf(), level=level)
    assert not Document.objects.exists()  # refused before anything is stored
    services.submit(item=item, user=team.student, upload=_pdf(), level=L.PUBLIC)
    item.refresh_from_db()
    assert item.document is not None
    assert item.document.confidentiality == L.PUBLIC


def test_a_required_attachment_is_required(team: SimpleNamespace) -> None:
    item = _item(team, attachment_required=True)
    with pytest.raises(ValidationError):
        services.submit(item=item, user=team.student)
    assert RequiredItem.objects.get(pk=item.pk).status == S.TODO


def test_upload_and_document_are_exclusive(team: SimpleNamespace) -> None:
    item = _item(team)
    with pytest.raises(ValueError, match="exclusive"):
        services.submit(item=item, user=team.student, upload=_pdf(), document=_store(team))


def test_resubmission_adds_a_version_to_the_same_document(team: SimpleNamespace) -> None:
    item = _item(team)
    services.submit(item=item, user=team.student, upload=_pdf("v1"))
    services.reject(item=item, user=team.director, reason=Reason.ILLEGIBLE)
    services.submit(item=item, user=team.student, upload=_pdf("v2"))
    item.refresh_from_db()
    assert item.document is not None
    assert item.document.current_version == 2
    assert item.submitted_version == 2
    assert Document.objects.count() == 1


def test_an_existing_document_of_the_thesis_can_be_attached(team: SimpleNamespace) -> None:
    document = _store(team)
    item = _item(team)
    services.submit(item=item, user=team.student, document=document)
    item.refresh_from_db()
    assert (item.document_id, item.submitted_version) == (document.pk, 1)


def test_a_document_of_another_thesis_is_refused(team: SimpleNamespace) -> None:
    """Complement 2: document.thesis_id == item.thesis_id, otherwise refused."""
    other = make_team()
    foreign = _store(other, level=L.PUBLIC)
    # The student of this thesis directs the other one and CAN read the foreign
    # document: only the thesis check stops the attachment.
    from apps.accounts.permissions import can_access_document
    from apps.accounts.tests.factories import MembershipFactory

    MembershipFactory.create(thesis=other.thesis, role=Role.DIRECTOR, user=team.student)
    assert can_access_document(team.student, foreign)
    item = _item(team, default_level=L.PUBLIC)
    with pytest.raises(PermissionDenied):
        services.submit(item=item, user=other.student, document=foreign)
    with pytest.raises(PermissionDenied):
        services.submit(item=item, user=team.student, document=foreign)
    item.refresh_from_db()
    assert (item.status, item.document_id) == (S.TODO, None)
    event = AuditEvent.objects.filter(action="ADMIN_ITEM_ACCESS_DENIED").order_by("-seq").first()
    assert event is not None
    assert event.details_json["reason"] == "foreign_document"


def test_a_document_attached_to_another_item_is_refused(team: SimpleNamespace) -> None:
    document = _store(team)
    first, second = _item(team), _item(team)
    services.submit(item=first, user=team.student, document=document)
    with pytest.raises(ValidationError):
        services.submit(item=second, user=team.student, document=document)
    assert RequiredItem.objects.get(pk=second.pk).document_id is None


def test_one_document_belongs_to_one_item_in_the_database(team: SimpleNamespace) -> None:
    """Complement 1: unique(document) when not NULL (the OneToOneField's UNIQUE)."""
    document = _store(team)
    first, second = _item(team), _item(team)  # two NULL documents: fine
    RequiredItem.objects.filter(pk=first.pk).update(document=document)
    with pytest.raises(IntegrityError), transaction.atomic():
        RequiredItem.objects.filter(pk=second.pk).update(document=document)


def test_a_document_at_a_disallowed_level_is_refused(team: SimpleNamespace) -> None:
    document = _store(team, level=L.INTERNAL_ST)  # the director could not read it
    item = _item(team, validator_roles=[D])
    with pytest.raises(ValidationError):
        services.submit(item=item, user=team.student, document=document)


def test_approval_refuses_a_content_changed_since_submission(team: SimpleNamespace) -> None:
    item = _item(team)
    services.submit(item=item, user=team.student, upload=_pdf("v1"))
    item.refresh_from_db()
    assert item.document is not None
    vault.add_version(document=item.document, upload=_pdf("v2"), user=team.student)
    with pytest.raises(StaleSubmissionError):
        services.approve(item=item, user=team.director)
    assert RequiredItem.objects.get(pk=item.pk).status == S.SUBMITTED


def test_approval_refuses_an_attachment_the_validator_cannot_read(team: SimpleNamespace) -> None:
    item = _item(team, validator_roles=[D])
    services.submit(item=item, user=team.student, upload=_pdf())
    item.refresh_from_db()
    assert item.document is not None
    vault.change_level(document=item.document, level=L.INTERNAL_ST, user=team.student)
    with pytest.raises(PermissionDenied):
        services.approve(item=item, user=team.director)
    event = AuditEvent.objects.filter(action="ADMIN_ITEM_ACCESS_DENIED").order_by("-seq").first()
    assert event is not None
    assert event.details_json["reason"] == "unreadable_document"


def test_the_approval_names_the_submitted_version(team: SimpleNamespace) -> None:
    item = _item(team)
    services.submit(item=item, user=team.student, upload=_pdf())
    services.approve(item=item, user=team.director)
    approval = item.approvals.get()
    assert approval.version == 1


def test_admin_file_never_touches_storage_keys_or_crypto() -> None:
    """Attachments go through vault.services only (plan, section 5)."""
    forbidden = {
        "apps.vault.storage",
        "apps.vault.crypto",
        "apps.vault.keys",
        "cryptography",
        "nacl",
        "hashlib",
    }
    root = Path(__file__).resolve().parents[1]
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                names = [base, *(f"{base}.{a.name}" for a in node.names)]
            for name in names:
                assert not any(name == f or name.startswith(f + ".") for f in forbidden), (
                    path,
                    name,
                )
