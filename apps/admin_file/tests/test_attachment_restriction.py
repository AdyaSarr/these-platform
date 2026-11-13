"""A document attached to a RequiredItem is readable only by holders of the read
right of the item's section (CIFRE_FILE_READ or ADMIN_FILE_READ), on top of the
M2 rule. Checked through the real Documents pages (list, detail, download).

transaction=True (CLAUDE.md): signing in is audited, then the vault stores.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts import permissions
from apps.accounts.permissions import can_access_document
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory
from apps.admin_file.access import attachment_restriction
from apps.admin_file.models import RequiredItem, Section
from apps.admin_file.services import seed_items
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault import services as vault
from apps.vault.models import Document

pytestmark = pytest.mark.django_db(transaction=True)

ROLES = ("student", "director", "co_supervisor", "tutor", "csi", "guest")


def _pdf() -> bytes:
    return b"%PDF-1.7\n%Piece fictive\n%%EOF\n"


@pytest.fixture
def team() -> SimpleNamespace:
    thesis = ThesisFactory.create()

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(thesis=thesis, role=role, **kwargs).user

    team = SimpleNamespace(
        thesis=thesis,
        student=member(Role.PHD_STUDENT),
        director=member(Role.DIRECTOR),
        co_supervisor=member(Role.CO_SUPERVISOR),
        tutor=member(Role.INDUSTRIAL_TUTOR),
        csi=member(Role.CSI_MEMBER, active_until=timezone.now() + timedelta(days=60)),
        guest=member(Role.GUEST),
    )
    seed_items(thesis)
    return team


def _attach(team: SimpleNamespace, code: str, level: L | None = None) -> Document:
    """Store a fictitious piece and attach it (the submit service comes next commit)."""
    item = RequiredItem.objects.get(thesis=team.thesis, code=code)
    document = vault.store(
        thesis=team.thesis,
        upload=SimpleUploadedFile("piece.pdf", _pdf()),
        title="Dossier administratif - pièce fictive",
        level=level or L(item.default_level),
        user=team.student,
    )
    RequiredItem.objects.filter(pk=item.pk).update(document=document)
    return document


def _client(user: Any) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _list_ids(team: SimpleNamespace, user: Any) -> set[Any]:
    response = _client(user).get(reverse("vault:documents", kwargs={"thesis_id": team.thesis.pk}))
    assert response.status_code == 200
    return {d.pk for d in response.context["documents"]}


def _detail(team: SimpleNamespace, user: Any, document: Document) -> int:
    url = reverse(
        "vault:document", kwargs={"thesis_id": team.thesis.pk, "document_id": document.pk}
    )
    return _client(user).get(url).status_code


def _download(team: SimpleNamespace, user: Any, document: Document) -> Any:
    version = document.versions.get(version=1)
    url = reverse("vault:download", kwargs={"thesis_id": team.thesis.pk, "version_id": version.pk})
    return _client(user).get(url)


def test_the_restriction_is_registered() -> None:
    assert attachment_restriction in permissions._DOCUMENT_RESTRICTIONS


@pytest.mark.parametrize("code", ["adum_enrollment", "fsd_zrr_clearance"])
def test_co_supervisor_can_neither_list_nor_open_nor_download(
    team: SimpleNamespace, code: str
) -> None:
    """INTERNAL_LIRMM would let a co-supervisor in; the section right does not."""
    document = _attach(team, code)
    assert document.pk not in _list_ids(team, team.co_supervisor)
    assert _detail(team, team.co_supervisor, document) == 403
    assert _download(team, team.co_supervisor, document).status_code == 403


@pytest.mark.parametrize("code", ["adum_enrollment", "fsd_zrr_clearance"])
def test_director_and_student_can_list_open_and_download(team: SimpleNamespace, code: str) -> None:
    document = _attach(team, code)
    for user in (team.director, team.student):
        assert document.pk in _list_ids(team, user)
        assert _detail(team, user, document) == 200
        response = _download(team, user, document)
        assert response.status_code == 200
        assert response.content == _pdf()


def test_the_refusal_is_audited(team: SimpleNamespace) -> None:
    document = _attach(team, "adum_enrollment")
    _download(team, team.co_supervisor, document)
    from apps.vault.models import AuditEvent

    assert AuditEvent.objects.filter(
        action="DOCUMENT_ACCESS_DENIED", actor=team.co_supervisor
    ).exists()


@pytest.mark.parametrize(
    ("code", "readers"),
    [
        ("adum_enrollment", {"student", "director"}),  # ACADEMIC, INTERNAL_LIRMM
        ("employment_contract", {"student", "tutor"}),  # CIFRE, INTERNAL_ST
        ("collaboration_contract", {"student", "director", "tutor"}),  # CIFRE, CONFIDENTIAL
    ],
)
def test_readers_are_the_intersection_of_level_and_section(
    team: SimpleNamespace, code: str, readers: set[str]
) -> None:
    document = _attach(team, code)
    actual = {role for role in ROLES if can_access_document(getattr(team, role), document)}
    assert actual == readers


def test_section_wins_over_a_wider_level(team: SimpleNamespace) -> None:
    """An ACADEMIC piece stored as CONFIDENTIAL stays closed to the tutor."""
    document = _attach(team, "doctoral_charter", level=L.CONFIDENTIAL)
    assert not can_access_document(team.tutor, document)
    assert can_access_document(team.director, document)


def test_lowering_the_level_does_not_open_the_section(team: SimpleNamespace) -> None:
    document = _attach(team, "cvec_certificate")
    vault.change_level(document=document, level=L.PUBLIC, user=team.student)
    document.refresh_from_db()
    for role in ("co_supervisor", "tutor"):
        assert not can_access_document(getattr(team, role), document), role
        assert document.pk not in _list_ids(team, getattr(team, role))


def test_an_explicit_grant_does_not_bypass_the_section(team: SimpleNamespace) -> None:
    document = _attach(team, "collaboration_contract")
    vault.grant_access(document=document, grantee=team.co_supervisor, user=team.student)
    assert not can_access_document(team.co_supervisor, document)


def test_documents_not_attached_are_untouched(team: SimpleNamespace) -> None:
    """M2 behaviour for every other document (all M2 tests also stay green)."""
    document = vault.store(
        thesis=team.thesis,
        upload=SimpleUploadedFile("cr.pdf", _pdf()),
        title="Compte rendu fictif",
        level=L.INTERNAL_LIRMM,
        user=team.student,
    )
    assert document.pk in _list_ids(team, team.co_supervisor)
    assert _download(team, team.co_supervisor, document).status_code == 200


def test_an_item_of_another_thesis_cannot_restrict_or_open(team: SimpleNamespace) -> None:
    """The section right is checked on the DOCUMENT's thesis."""
    document = _attach(team, "adum_enrollment")
    other = MembershipFactory.create(role=Role.DIRECTOR)
    assert not can_access_document(other.user, document)


def test_section_mapping_covers_every_section() -> None:
    from apps.admin_file.models import SECTION_READ_ACTION

    assert set(SECTION_READ_ACTION) == set(Section)
