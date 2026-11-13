"""accounts.permissions.can_access_document: role AND level, or an explicit grant."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from apps.accounts.models import Thesis, User
from apps.accounts.permissions import LEVEL_ROLES, ROLE_ACTIONS, Action, can_access_document
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault.models import Document
from apps.vault.tests.factories import DocumentAccessGrantFactory, DocumentFactory

pytestmark = pytest.mark.django_db


def _member(role: Role, thesis: Thesis, **kwargs: object) -> User:
    return MembershipFactory.create(role=role, thesis=thesis, **kwargs).user


def _document(level: L, thesis: Thesis | None = None) -> Document:
    return DocumentFactory.create(confidentiality=level, thesis=thesis or ThesisFactory.create())


@pytest.mark.parametrize("level", list(L))
@pytest.mark.parametrize("role", list(Role))
def test_role_and_level_matrix(role: Role, level: L) -> None:
    document = _document(level)
    user = _member(role, document.thesis)
    expected = Action.DOCUMENT_READ in ROLE_ACTIONS[role] and role in LEVEL_ROLES[level]
    assert can_access_document(user, document) is expected


def test_outsider_anonymous_and_other_thesis_are_refused() -> None:
    document = _document(L.PUBLIC)
    other = _member(Role.PHD_STUDENT, ThesisFactory.create())
    assert not can_access_document(UserFactory.create(), document)
    assert not can_access_document(AnonymousUser(), document)
    assert not can_access_document(other, document)


def test_explicit_grant_opens_one_confidential_document_to_a_csi_member() -> None:
    document = _document(L.CONFIDENTIAL)
    csi = _member(
        Role.CSI_MEMBER, document.thesis, active_until=timezone.now() + timedelta(days=60)
    )
    assert not can_access_document(csi, document)
    DocumentAccessGrantFactory.create(document=document, user=csi)
    assert can_access_document(csi, document)
    other = _document(L.CONFIDENTIAL, thesis=document.thesis)
    assert not can_access_document(csi, other)  # per document


def test_revoked_grant_gives_nothing() -> None:
    document = _document(L.CONFIDENTIAL)
    csi = _member(
        Role.CSI_MEMBER, document.thesis, active_until=timezone.now() + timedelta(days=60)
    )
    DocumentAccessGrantFactory.create(document=document, user=csi, revoked_at=timezone.now())
    assert not can_access_document(csi, document)


def test_grant_needs_an_active_membership() -> None:
    document = _document(L.CONFIDENTIAL)
    past = timezone.now() - timedelta(days=90)
    csi = _member(
        Role.CSI_MEMBER, document.thesis, active_from=past, active_until=past + timedelta(days=60)
    )
    DocumentAccessGrantFactory.create(document=document, user=csi)
    assert not can_access_document(csi, document)
    outsider = UserFactory.create()
    DocumentAccessGrantFactory.create(document=document, user=outsider)
    assert not can_access_document(outsider, document)


@pytest.mark.parametrize("level", [L.PUBLIC, L.INTERNAL_LIRMM, L.INTERNAL_ST])
def test_grant_counts_only_for_confidential_documents(level: L) -> None:
    document = _document(level)
    guest = _member(Role.GUEST, document.thesis)
    DocumentAccessGrantFactory.create(document=document, user=guest)
    assert not can_access_document(guest, document)


def test_deactivated_user_is_refused_even_with_a_grant() -> None:
    document = _document(L.CONFIDENTIAL)
    guest = _member(Role.GUEST, document.thesis)
    DocumentAccessGrantFactory.create(document=document, user=guest)
    guest.is_active = False
    guest.save()
    assert not can_access_document(guest, document)


def test_unsaved_document_is_refused() -> None:
    thesis = ThesisFactory.create()
    student = _member(Role.PHD_STUDENT, thesis)
    assert not can_access_document(student, Document(thesis=thesis, confidentiality=L.PUBLIC))
