"""Document restrictions registered by modules (permissions.register_document_restriction).

can_access_document() = the M2 rule (role AND level, or explicit grant) AND every
registered restriction. With no restriction, M2's behaviour is unchanged.
These tests use a private registry (monkeypatched) so that they do not depend on
which modules are installed; apps/admin_file/tests covers the real restriction.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.accounts import permissions
from apps.accounts.permissions import can_access_document, register_document_restriction
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault.tests.factories import DocumentAccessGrantFactory, DocumentFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    empty: list[Any] = []
    monkeypatch.setattr(permissions, "_DOCUMENT_RESTRICTIONS", empty)
    return empty


def _member(document: Any, role: Role) -> Any:
    return MembershipFactory.create(thesis=document.thesis, role=role).user


def test_without_restrictions_the_m2_rule_is_unchanged(registry: list[Any]) -> None:
    document = DocumentFactory.create(confidentiality=L.INTERNAL_LIRMM)
    assert can_access_document(_member(document, Role.CO_SUPERVISOR), document)
    assert not can_access_document(_member(document, Role.INDUSTRIAL_TUTOR), document)


def test_a_refusing_restriction_denies_an_otherwise_allowed_reader(registry: list[Any]) -> None:
    document = DocumentFactory.create(confidentiality=L.PUBLIC)
    reader = _member(document, Role.DIRECTOR)
    register_document_restriction(lambda user, doc: False)
    assert not can_access_document(reader, document)


def test_every_restriction_must_accept(registry: list[Any]) -> None:
    document = DocumentFactory.create(confidentiality=L.PUBLIC)
    reader = _member(document, Role.DIRECTOR)
    register_document_restriction(lambda user, doc: True)
    assert can_access_document(reader, document)
    register_document_restriction(lambda user, doc: doc.pk != document.pk)
    assert not can_access_document(reader, document)


def test_a_restriction_never_grants(registry: list[Any]) -> None:
    """A restriction answering True cannot open a document the M2 rule refuses."""
    document = DocumentFactory.create(confidentiality=L.INTERNAL_ST)
    outsider = _member(document, Role.DIRECTOR)
    register_document_restriction(lambda user, doc: True)
    assert not can_access_document(outsider, document)


def test_restrictions_also_apply_to_explicit_grants(registry: list[Any]) -> None:
    grant = DocumentAccessGrantFactory.create()
    MembershipFactory.create(user=grant.user, thesis=grant.document.thesis, role=Role.CO_SUPERVISOR)
    assert can_access_document(grant.user, grant.document)
    register_document_restriction(lambda user, doc: False)
    assert not can_access_document(grant.user, grant.document)


def test_restrictions_receive_user_and_document(registry: list[Any]) -> None:
    document = DocumentFactory.create(confidentiality=L.PUBLIC)
    reader = _member(document, Role.DIRECTOR)
    seen: list[tuple[Any, Any]] = []

    def spy(user: Any, doc: Any) -> bool:
        seen.append((user, doc))
        return True

    register_document_restriction(spy)
    assert can_access_document(reader, document)
    assert seen == [(reader, document)]


def test_a_failing_restriction_never_allows(registry: list[Any]) -> None:
    """An exception propagates (500), it is never read as an acceptance."""
    document = DocumentFactory.create(confidentiality=L.PUBLIC)
    reader = _member(document, Role.DIRECTOR)

    def broken(user: Any, doc: Any) -> bool:
        raise RuntimeError("restriction failed")

    register_document_restriction(broken)
    with pytest.raises(RuntimeError):
        can_access_document(reader, document)


def test_registration_is_idempotent(registry: list[Any]) -> None:
    """AppConfig.ready() may run more than once."""

    def restriction(user: Any, doc: Any) -> bool:
        return True

    register_document_restriction(restriction)
    register_document_restriction(restriction)
    assert registry == [restriction]


def test_registration_refuses_a_non_callable(registry: list[Any]) -> None:
    with pytest.raises(ImproperlyConfigured):
        register_document_restriction("not callable")  # type: ignore[arg-type]
