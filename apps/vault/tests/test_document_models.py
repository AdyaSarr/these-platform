"""Document models: what is stored in clear, and what the database refuses."""

from typing import Any

import pytest
from django.db import DatabaseError, transaction
from django.db.models import ProtectedError

from apps.accounts.tests.factories import UserFactory
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault import keys
from apps.vault.errors import KekRetirementError
from apps.vault.formats import KeyDomain
from apps.vault.models import Document, DocumentAccessGrant, DocumentVersion, KeyEncryptionKey
from apps.vault.tests.factories import (
    DocumentAccessGrantFactory,
    DocumentFactory,
    DocumentVersionFactory,
)

# KEK rows are created by the tests themselves (no automatic version 1).
pytestmark = [pytest.mark.django_db, pytest.mark.without_vault_keys]


@pytest.fixture(autouse=True)
def _no_retire_grace(settings: Any) -> None:
    from datetime import timedelta

    settings.VAULT_RETIRE_GRACE = timedelta(0)


def _refused(**fields: Any) -> None:
    with pytest.raises(DatabaseError), transaction.atomic():
        DocumentVersionFactory.create(**fields)


# --------------------------------------------------------------------------- stored in clear


def test_document_version_stores_no_plaintext_digest_nor_redundant_crypto_fields() -> None:
    names = {f.name for f in DocumentVersion._meta.get_fields()}
    assert names == {
        "id",
        "document",
        "version",
        "ciphertext_path",
        "wrapped_dek",
        "kek_id",
        "size",
        "mime_type",
        "created_at",
        "updated_at",
        "created_by",
    }
    assert not names & {"sha256_of_plaintext", "nonce", "algorithm", "content", "dek"}


def test_document_fields() -> None:
    names = {f.name for f in Document._meta.get_fields() if not f.auto_created}
    assert names == {
        "id",
        "thesis",
        "title",
        "confidentiality",
        "owner",
        "current_version",
        "related_content_type",
        "related_object_id",
        "related_object",
        "created_at",
        "updated_at",
        "created_by",
    }


def test_str_and_repr_never_show_the_wrapped_dek() -> None:
    version = DocumentVersionFactory.create()
    for text in (str(version), repr(version), str(version.document)):
        assert version.wrapped_dek.hex() not in text
        assert repr(bytes(version.wrapped_dek)) not in text


# --------------------------------------------------------------------------- constraints


def test_one_row_per_document_and_version() -> None:
    version = DocumentVersionFactory.create()
    _refused(document=version.document, version=1)


@pytest.mark.parametrize("version", [0, 2**32])
def test_version_range(version: int) -> None:
    _refused(version=version)


@pytest.mark.parametrize(
    "path",
    [
        "../etc/passwd",
        "/abs/0123456789abcdef0123456789abcdef.blob",
        "ab/0123456789abcdef0123456789abcdef.txt",
        "AB/AB23456789abcdef0123456789abcdef.blob",
        "ab/cd/0123456789abcdef0123456789abcdef.blob",
        "0123456789abcdef0123456789abcdef.blob",
        "cd/ab23456789abcdef0123456789abcdef.blob",  # shard is not the name's prefix
    ],
)
def test_ciphertext_path_must_follow_the_storage_layout(path: str) -> None:
    _refused(ciphertext_path=path)


def test_ciphertext_path_is_unique() -> None:
    version = DocumentVersionFactory.create()
    _refused(ciphertext_path=version.ciphertext_path)


@pytest.mark.parametrize("length", [0, 59, 61, 32])
def test_wrapped_dek_is_exactly_60_bytes(length: int) -> None:
    _refused(wrapped_dek=b"\x00" * length)


@pytest.mark.parametrize("kek_id", ["", "env:CONFIDENTIAL:01", "env:NOPE:1", "x" * 65])
def test_kek_id_format_is_checked(kek_id: str) -> None:
    _refused(kek_id=kek_id)


@pytest.mark.parametrize("mime", ["text/html", "application/x-msdownload", "image/svg+xml", ""])
def test_only_whitelisted_mime_types(mime: str) -> None:
    _refused(mime_type=mime)


def test_document_level_must_be_known() -> None:
    with pytest.raises(DatabaseError), transaction.atomic():
        DocumentFactory.create(confidentiality="SECRET")


def test_current_version_is_at_least_one() -> None:
    with pytest.raises(DatabaseError), transaction.atomic():
        DocumentFactory.create(current_version=0)


def test_documents_and_versions_are_protected() -> None:
    version = DocumentVersionFactory.create()
    with pytest.raises(ProtectedError):
        version.document.delete()
    with pytest.raises(ProtectedError):
        version.document.thesis.delete()


# --------------------------------------------------------------------------- grants


def test_one_active_grant_per_document_and_user() -> None:
    grant = DocumentAccessGrantFactory.create()
    with pytest.raises(DatabaseError), transaction.atomic():
        DocumentAccessGrantFactory.create(document=grant.document, user=grant.user)


def test_a_revoked_grant_can_be_granted_again() -> None:
    from django.utils import timezone

    grant = DocumentAccessGrantFactory.create(revoked_at=timezone.now())
    DocumentAccessGrantFactory.create(document=grant.document, user=grant.user)
    assert DocumentAccessGrant.objects.filter(document=grant.document).count() == 2


def test_grant_cannot_be_given_to_oneself_by_the_database() -> None:
    user = UserFactory.create(mfa=False)
    with pytest.raises(DatabaseError), transaction.atomic():
        DocumentAccessGrantFactory.create(user=user, granted_by=user)


# --------------------------------------------------------------------------- KEK references


def test_document_domains_have_a_reference_counter() -> None:
    """Registered in VaultConfig.ready(): a referenced document KEK cannot be retired."""
    for level in L:
        KeyEncryptionKey.objects.create(provider="env", domain=level.value, version=1)
        KeyEncryptionKey.objects.create(provider="env", domain=level.value, version=2)
        kek_id = f"env:{level.value}:1"
        DocumentVersionFactory.create(document__confidentiality=level, kek_id=kek_id)
        with pytest.raises(KekRetirementError, match="referenced by 1"):
            keys.retire(kek_id)


def test_unreferenced_document_kek_can_be_retired() -> None:
    KeyEncryptionKey.objects.create(provider="env", domain=KeyDomain.PUBLIC, version=1)
    KeyEncryptionKey.objects.create(provider="env", domain=KeyDomain.PUBLIC, version=2)
    keys.retire("env:PUBLIC:1")
    assert KeyEncryptionKey.objects.get(kek_id="env:PUBLIC:1").retired_at is not None
