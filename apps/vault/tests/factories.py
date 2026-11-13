"""Factories for vault models. Fictitious data only (CLAUDE.md rule 10).

DocumentVersionFactory makes database rows only: its wrapped_dek is random
bytes and no blob exists on disk. Tests of encryption go through the services.
"""

import secrets

import factory

from apps.accounts.tests.factories import ThesisFactory, UserFactory
from apps.core.confidentiality import ConfidentialityLevel
from apps.vault.models import Document, DocumentAccessGrant, DocumentVersion


def _blob_path(name: str) -> str:
    return f"{name[:2]}/{name}.blob"  # layout of vault.storage


class DocumentFactory(factory.django.DjangoModelFactory[Document]):
    class Meta:
        model = Document

    thesis = factory.SubFactory(ThesisFactory)
    title = factory.Sequence(lambda n: f"Document fictif {n}")
    confidentiality = ConfidentialityLevel.INTERNAL_LIRMM
    owner = factory.SubFactory(UserFactory, mfa=False)  # no TOTP: no KEK needed
    current_version = 1


class DocumentVersionFactory(factory.django.DjangoModelFactory[DocumentVersion]):
    class Meta:
        model = DocumentVersion

    document = factory.SubFactory(DocumentFactory)
    version = 1
    ciphertext_path = factory.LazyFunction(lambda: _blob_path(secrets.token_hex(16)))
    wrapped_dek = factory.LazyFunction(lambda: secrets.token_bytes(60))
    kek_id = "env:INTERNAL_LIRMM:1"
    size = 1234
    mime_type = "application/pdf"


class DocumentAccessGrantFactory(factory.django.DjangoModelFactory[DocumentAccessGrant]):
    class Meta:
        model = DocumentAccessGrant

    document = factory.SubFactory(
        DocumentFactory, confidentiality=ConfidentialityLevel.CONFIDENTIAL
    )
    user = factory.SubFactory(UserFactory, mfa=False)
    granted_by = factory.SubFactory(UserFactory, mfa=False)
