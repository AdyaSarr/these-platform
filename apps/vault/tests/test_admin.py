"""The vault in the Django admin: read-only, metadata only (no title, no DEK, no clear)."""

import secrets
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, UserFactory
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault import checkpoints, services
from apps.vault.models import (
    AuditCheckpoint,
    AuditEvent,
    Document,
    DocumentAccessGrant,
    DocumentVersion,
    KeyEncryptionKey,
)

pytestmark = pytest.mark.django_db(transaction=True)

MODELS = [
    Document,
    DocumentVersion,
    DocumentAccessGrant,
    KeyEncryptionKey,
    AuditEvent,
    AuditCheckpoint,
]


@pytest.fixture
def setup() -> dict[str, Any]:
    marker = "TEMOIN-" + secrets.token_hex(6)
    student = MembershipFactory.create(role=Role.PHD_STUDENT).user
    thesis = student.memberships.get().thesis
    csi = MembershipFactory.create(thesis=thesis, role=Role.CSI_MEMBER).user
    document = services.store(
        thesis=thesis,
        upload=SimpleUploadedFile("r.pdf", f"%PDF-1.7\n%{marker}\n".encode()),
        title=f"Titre {marker}",
        level=L.CONFIDENTIAL,
        user=student,
    )
    services.grant_access(document=document, grantee=csi, user=student)
    checkpoints.create_checkpoint()
    admin = UserFactory.create(is_staff=True, is_superuser=True)
    client = Client()
    client.force_login(admin)
    return {"client": client, "marker": marker, "version": document.versions.get()}


def _admin_url(model: Any, view: str, *args: Any) -> str:
    return reverse(f"admin:vault_{model._meta.model_name}_{view}", args=args)


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_pages_show_no_title_no_dek_no_clear(setup: dict[str, Any], model: Any) -> None:
    client, marker = setup["client"], setup["marker"]
    wrapped = bytes(setup["version"].wrapped_dek)
    listing = client.get(_admin_url(model, "changelist"))
    assert listing.status_code == 200
    pages = [listing.content.decode()]
    for obj in model.objects.all()[:3]:
        page = client.get(_admin_url(model, "change", obj.pk))
        assert page.status_code == 200
        pages.append(page.content.decode())
    for page in pages:
        assert marker not in page  # neither the title nor the content
        assert wrapped.hex() not in page
        assert "wrapped_dek" not in page


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_nothing_can_be_added_changed_or_deleted(setup: dict[str, Any], model: Any) -> None:
    client = setup["client"]
    obj = model.objects.first()
    assert client.get(_admin_url(model, "add")).status_code == 403
    assert client.post(_admin_url(model, "change", obj.pk), {}).status_code == 403
    assert client.post(_admin_url(model, "delete", obj.pk), {"post": "yes"}).status_code == 403
    assert model.objects.filter(pk=obj.pk).exists()
