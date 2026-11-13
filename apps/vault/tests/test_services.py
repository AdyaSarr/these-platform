"""vault.services: encrypted documents, access, audit, level change, grants, MFA secrets."""

import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.utils import timezone

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault import keys, services, storage
from apps.vault.errors import (
    BlobNotFoundError,
    StorageError,
    UnknownKekError,
    VaultIntegrityError,
)
from apps.vault.formats import KeyDomain
from apps.vault.models import (
    AuditEvent,
    Document,
    DocumentAccessGrant,
    DocumentVersion,
    KeyEncryptionKey,
)

# transaction=True: every service call commits, as in production (no
# ATOMIC_REQUESTS). In one enclosing test transaction, the audit() of a first
# call would precede the KEK lock of the next one, which the lock order forbids.
pytestmark = pytest.mark.django_db(transaction=True)

PDF = b"%PDF-1.7\n%Compte rendu fictif, contenu reconnaissable XYZZY-42\n%%EOF\n"
PDF2 = b"%PDF-1.7\n%Seconde version fictive, PLUGH-17\n%%EOF\n"
TXT = "Notes fictives : réunion de suivi.\n".encode()


def _pdf(data: bytes = PDF, name: str = "rapport.pdf") -> Any:
    return SimpleUploadedFile(name, data, content_type="application/pdf")


@pytest.fixture
def team() -> SimpleNamespace:
    thesis = ThesisFactory.create()

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(thesis=thesis, role=role, **kwargs).user

    return SimpleNamespace(
        thesis=thesis,
        student=member(Role.PHD_STUDENT),
        director=member(Role.DIRECTOR),
        co_supervisor=member(Role.CO_SUPERVISOR),
        tutor=member(Role.INDUSTRIAL_TUTOR),
        csi=member(Role.CSI_MEMBER, active_until=timezone.now() + timedelta(days=60)),
        guest=member(Role.GUEST),
        outsider=UserFactory.create(),
    )


def _store(team: SimpleNamespace, level: L = L.INTERNAL_LIRMM, data: bytes = PDF) -> Document:
    return services.store(
        thesis=team.thesis,
        upload=_pdf(data),
        title="Compte rendu fictif",
        level=level,
        user=team.student,
    )


def _v(document: Document, number: int = 1) -> DocumentVersion:
    return DocumentVersion.objects.get(document=document, version=number)


def _events(action: str) -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=action).order_by("seq"))


def _blob_file(version: DocumentVersion) -> Path:
    return storage.root() / version.ciphertext_path


# --------------------------------------------------------------------------- store


@pytest.mark.parametrize("level", list(L))
def test_store_then_open_round_trip(team: SimpleNamespace, level: L) -> None:
    document = _store(team, level)
    opened = services.open_version(_v(document).pk, team.student)
    assert opened.data == PDF
    assert opened.mime_type == "application/pdf"
    assert opened.download_name == "compte-rendu-fictif.pdf"
    version = _v(document)
    assert version.kek_id == f"env:{level.value}:1"
    assert version.size == len(PDF)


def test_blob_on_disk_is_not_the_plaintext(team: SimpleNamespace) -> None:
    raw = _blob_file(_v(_store(team))).read_bytes()
    assert raw[0] == 0x01
    assert b"XYZZY-42" not in raw
    assert PDF[:16] not in raw


def test_database_holds_no_plaintext_nor_dek(team: SimpleNamespace) -> None:
    version = _v(_store(team))
    row = str(DocumentVersion.objects.filter(pk=version.pk).values().get())
    assert "XYZZY" not in row
    assert len(bytes(version.wrapped_dek)) == 60


def test_store_is_audited_last(team: SimpleNamespace) -> None:
    document = _store(team)
    last = AuditEvent.objects.order_by("-seq").first()
    assert last is not None
    assert last.action == "DOCUMENT_CREATED"
    assert last.object_id == str(document.pk)
    assert last.thesis == team.thesis
    assert last.actor == team.student


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor", "csi", "guest", "outsider"])
def test_only_the_student_stores(team: SimpleNamespace, role: str) -> None:
    with pytest.raises(PermissionDenied):
        services.store(
            thesis=team.thesis, upload=_pdf(), title="x", level=L.PUBLIC, user=getattr(team, role)
        )
    assert not Document.objects.exists()
    assert list(storage.root().rglob("*.blob")) == []
    (denied,) = _events("DOCUMENT_ACCESS_DENIED")
    assert denied.details_json["operation"] == "store"


def test_invalid_upload_writes_nothing(team: SimpleNamespace) -> None:
    with pytest.raises(ValidationError):
        services.store(
            thesis=team.thesis,
            upload=_pdf(name="rapport.png"),
            title="x",
            level=L.PUBLIC,
            user=team.student,
        )
    assert not Document.objects.exists()
    assert list(storage.root().rglob("*.blob")) == []


@pytest.mark.parametrize("title", ["", "   "])
def test_empty_title_is_refused(team: SimpleNamespace, title: str) -> None:
    with pytest.raises(ValidationError):
        services.store(
            thesis=team.thesis, upload=_pdf(), title=title, level=L.PUBLIC, user=team.student
        )


def test_failure_after_the_blob_leaves_only_an_unreadable_orphan(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_wrap(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(keys, "wrap", broken_wrap)
    with pytest.raises(RuntimeError):
        _store(team)
    assert not Document.objects.exists()
    assert not DocumentVersion.objects.exists()
    assert len(list(storage.root().rglob("*.blob"))) == 1  # orphan, its DEK never stored
    assert not _events("DOCUMENT_CREATED")


# --------------------------------------------------------------------------- add_version


def test_add_version(team: SimpleNamespace) -> None:
    document = _store(team)
    services.add_version(document=document, upload=_pdf(PDF2), user=team.student)
    document.refresh_from_db()
    assert document.current_version == 2
    assert services.open_version(_v(document, 1).pk, team.student).data == PDF
    assert services.open_version(_v(document, 2).pk, team.student).data == PDF2
    assert _events("DOCUMENT_VERSION_ADDED")[-1].details_json["version"] == 2


def test_add_version_refused_for_a_reader(team: SimpleNamespace) -> None:
    document = _store(team)
    with pytest.raises(PermissionDenied):
        services.add_version(document=document, upload=_pdf(PDF2), user=team.director)
    assert document.versions.count() == 1


def test_blob_of_another_version_does_not_decrypt(team: SimpleNamespace) -> None:
    document = _store(team)
    services.add_version(document=document, upload=_pdf(PDF2), user=team.student)
    _blob_file(_v(document, 1)).write_bytes(_blob_file(_v(document, 2)).read_bytes())
    with pytest.raises(VaultIntegrityError):
        services.open_version(_v(document, 1).pk, team.student)


# --------------------------------------------------------------------------- open: rights


ROLES = ["student", "director", "co_supervisor", "tutor", "csi", "guest", "outsider"]


@pytest.mark.parametrize("level", list(L))
@pytest.mark.parametrize("role", ROLES)
def test_open_follows_can_access_document(team: SimpleNamespace, role: str, level: L) -> None:
    from apps.accounts.permissions import can_access_document

    document = _store(team, level)
    user = getattr(team, role)
    if can_access_document(user, document):
        assert services.open_version(_v(document).pk, user).data == PDF
        assert _events("DOCUMENT_READ")[-1].actor == user
    else:
        with pytest.raises(PermissionDenied):
            services.open_version(_v(document).pk, user)
        denied = _events("DOCUMENT_ACCESS_DENIED")[-1]
        assert denied.details_json["operation"] == "read"
        assert not _events("DOCUMENT_READ")


def test_expected_readers_of_each_level(team: SimpleNamespace) -> None:
    """Spot check against docs/ARCHITECTURE.md, independent of permissions.py."""
    readers = {
        L.PUBLIC: {"student", "director", "co_supervisor", "tutor"},
        L.INTERNAL_LIRMM: {"student", "director", "co_supervisor"},
        L.INTERNAL_ST: {"student", "tutor"},
        L.CONFIDENTIAL: {"student", "director", "tutor"},
    }
    for level, expected in readers.items():
        version_id = _v(_store(team, level)).pk
        allowed = set()
        for role in ROLES:
            try:
                services.open_version(version_id, getattr(team, role))
                allowed.add(role)
            except PermissionDenied:
                pass
        assert allowed == expected, level


def test_unknown_version_looks_like_a_refusal(team: SimpleNamespace) -> None:
    import uuid

    with pytest.raises(PermissionDenied):
        services.open_version(uuid.uuid4(), team.student)
    assert _events("DOCUMENT_ACCESS_DENIED")


# --------------------------------------------------------------------------- open: failures


def test_tampered_blob_is_an_integrity_failure(team: SimpleNamespace) -> None:
    version = _v(_store(team))
    path = _blob_file(version)
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 1
    path.write_bytes(bytes(raw))
    with pytest.raises(VaultIntegrityError):
        services.open_version(version.pk, team.student)
    assert len(_events("DOCUMENT_INTEGRITY_FAILED")) == 1
    assert not _events("DOCUMENT_KEK_UNAVAILABLE")
    assert not _events("DOCUMENT_READ")


def test_missing_blob_is_an_integrity_failure(team: SimpleNamespace) -> None:
    version = _v(_store(team))
    _blob_file(version).unlink()
    with pytest.raises(BlobNotFoundError):
        services.open_version(version.pk, team.student)
    assert len(_events("DOCUMENT_INTEGRITY_FAILED")) == 1


def test_level_changed_in_the_database_does_not_decrypt(team: SimpleNamespace) -> None:
    """Acceptance criterion: no level change without re-encryption."""
    document = _store(team, L.CONFIDENTIAL)
    Document.objects.filter(pk=document.pk).update(confidentiality=L.INTERNAL_ST)
    with pytest.raises(VaultIntegrityError):
        services.open_version(_v(document).pk, team.student)


def test_retired_kek_is_unavailable_not_an_integrity_failure(team: SimpleNamespace) -> None:
    version = _v(_store(team))
    KeyEncryptionKey.objects.filter(kek_id=version.kek_id).update(retired_at=timezone.now())
    with pytest.raises(UnknownKekError):
        services.open_version(version.pk, team.student)
    assert len(_events("DOCUMENT_KEK_UNAVAILABLE")) == 1
    assert not _events("DOCUMENT_INTEGRITY_FAILED")


def test_disabled_provider_is_unavailable_not_an_integrity_failure(
    team: SimpleNamespace, settings: Any
) -> None:
    version = _v(_store(team))
    settings.VAULT_ENABLED_PROVIDERS = frozenset({"bao"})
    with pytest.raises(UnknownKekError):
        services.open_version(version.pk, team.student)
    assert _events("DOCUMENT_KEK_UNAVAILABLE")
    assert not _events("DOCUMENT_INTEGRITY_FAILED")


def test_missing_storage_root_is_not_an_integrity_failure(
    team: SimpleNamespace, settings: Any, tmp_path: Path
) -> None:
    version = _v(_store(team))
    settings.VAULT_STORAGE_DIR = str(tmp_path / "elsewhere")
    with pytest.raises(StorageError):
        services.open_version(version.pk, team.student)
    assert not _events("DOCUMENT_INTEGRITY_FAILED")
    assert _events("DOCUMENT_STORAGE_ERROR")


@pytest.mark.parametrize(
    ("title", "mime", "expected"),
    [
        ("Compte rendu fictif", "application/pdf", "compte-rendu-fictif.pdf"),
        ("Thèse : état de l'art", "text/plain", "these-etat-de-lart.txt"),
        ("../../etc/passwd", "image/png", "etcpasswd.png"),
        ("???", "image/jpeg", "document.jpg"),
        ("a" * 300, "application/zip", "a" * 80 + ".zip"),
    ],
)
def test_download_name_is_rebuilt_from_the_title(title: str, mime: str, expected: str) -> None:
    assert services.download_name(title, mime) == expected


# --------------------------------------------------------------------------- open: one snapshot


def test_concurrent_level_change_never_causes_a_false_integrity_failure() -> None:
    """open() reads the version AND its document in one query: a change_level()
    committed while open() is running cannot pair the old DEK with the new level.
    Mutation target: two separate queries."""
    thesis = ThesisFactory.create()
    student = MembershipFactory.create(thesis=thesis, role=Role.PHD_STUDENT).user
    document = services.store(
        thesis=thesis, upload=_pdf(), title="fictif", level=L.CONFIDENTIAL, user=student
    )
    version_id = _v(document).pk
    first_read_done, level_changed = threading.Event(), threading.Event()
    outcome: dict[str, Any] = {}

    def pause_after_first_version_query(execute: Any, sql: str, *args: Any) -> Any:
        result = execute(sql, *args)
        if "vault_documentversion" in sql and not first_read_done.is_set():
            first_read_done.set()
            assert level_changed.wait(10)
        return result

    def reader() -> None:
        try:
            with connection.execute_wrapper(pause_after_first_version_query):
                outcome["data"] = services.open_version(version_id, student).data
        except BaseException as exc:  # reported below
            outcome["error"] = exc
        finally:
            connection.close()

    def writer() -> None:
        try:
            assert first_read_done.wait(10)
            services.change_level(document=document, level=L.INTERNAL_ST, user=student)
        except BaseException as exc:
            outcome["writer_error"] = exc
        finally:
            level_changed.set()
            connection.close()

    threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert "writer_error" not in outcome, outcome.get("writer_error")
    assert "error" not in outcome, repr(outcome.get("error"))
    assert outcome["data"] == PDF
    assert not AuditEvent.objects.filter(action="DOCUMENT_INTEGRITY_FAILED").exists()


# --------------------------------------------------------------------------- change_level


def test_change_level_re_encrypts_every_version(team: SimpleNamespace) -> None:
    document = _store(team, L.CONFIDENTIAL)
    services.add_version(document=document, upload=_pdf(PDF2), user=team.student)
    before = {v.version: (v.ciphertext_path, bytes(v.wrapped_dek)) for v in document.versions.all()}
    services.change_level(document=document, level=L.INTERNAL_ST, user=team.student)
    document.refresh_from_db()
    assert document.confidentiality == L.INTERNAL_ST
    for v in document.versions.all():
        assert v.kek_id == "env:INTERNAL_ST:1"
        assert v.ciphertext_path != before[v.version][0]  # new blob
        assert bytes(v.wrapped_dek) != before[v.version][1]  # new DEK
    assert services.open_version(_v(document, 1).pk, team.student).data == PDF
    assert services.open_version(_v(document, 2).pk, team.student).data == PDF2
    event = _events("DOCUMENT_LEVEL_CHANGED")[-1]
    assert event.details_json["old_level"] == "CONFIDENTIAL"
    assert event.details_json["new_level"] == "INTERNAL_ST"
    assert event.details_json["versions"] == 2


def test_change_level_changes_who_can_read(team: SimpleNamespace) -> None:
    document = _store(team, L.INTERNAL_LIRMM)
    services.open_version(_v(document).pk, team.director)
    services.change_level(document=document, level=L.INTERNAL_ST, user=team.student)
    with pytest.raises(PermissionDenied):
        services.open_version(_v(document).pk, team.director)
    assert services.open_version(_v(document).pk, team.tutor).data == PDF


def test_change_level_revokes_explicit_grants_when_leaving_confidential(
    team: SimpleNamespace,
) -> None:
    document = _store(team, L.CONFIDENTIAL)
    services.grant_access(document=document, grantee=team.csi, user=team.student)
    services.change_level(document=document, level=L.PUBLIC, user=team.student)
    assert not DocumentAccessGrant.objects.filter(revoked_at__isnull=True).exists()
    assert _events("DOCUMENT_LEVEL_CHANGED")[-1].details_json["grants_revoked"] == 1


def test_change_level_refused_for_a_reader_and_for_the_same_level(team: SimpleNamespace) -> None:
    document = _store(team, L.CONFIDENTIAL)
    with pytest.raises(PermissionDenied):
        services.change_level(document=document, level=L.PUBLIC, user=team.director)
    with pytest.raises(ValueError):
        services.change_level(document=document, level=L.CONFIDENTIAL, user=team.student)
    document.refresh_from_db()
    assert document.confidentiality == L.CONFIDENTIAL


# --------------------------------------------------------------------------- grants


def test_grant_opens_a_confidential_document_to_a_csi_member(team: SimpleNamespace) -> None:
    version_id = _v(_store(team, L.CONFIDENTIAL)).pk
    document = Document.objects.get()
    with pytest.raises(PermissionDenied):
        services.open_version(version_id, team.csi)
    grant = services.grant_access(document=document, grantee=team.csi, user=team.student)
    assert services.open_version(version_id, team.csi).data == PDF
    services.revoke_access(grant=grant, user=team.student)
    with pytest.raises(PermissionDenied):
        services.open_version(version_id, team.csi)
    assert len(_events("DOCUMENT_GRANT_ADDED")) == 1
    assert len(_events("DOCUMENT_GRANT_REVOKED")) == 1


def test_grant_rules(team: SimpleNamespace) -> None:
    confidential = _store(team, L.CONFIDENTIAL)
    internal = _store(team, L.INTERNAL_LIRMM)
    with pytest.raises(PermissionDenied):
        services.grant_access(document=confidential, grantee=team.csi, user=team.director)
    with pytest.raises(ValidationError):
        services.grant_access(document=internal, grantee=team.csi, user=team.student)
    with pytest.raises(ValidationError):
        services.grant_access(document=confidential, grantee=team.outsider, user=team.student)
    with pytest.raises(ValidationError):
        services.grant_access(document=confidential, grantee=team.student, user=team.student)
    assert not DocumentAccessGrant.objects.exists()


def test_granting_twice_keeps_one_active_grant(team: SimpleNamespace) -> None:
    document = _store(team, L.CONFIDENTIAL)
    first = services.grant_access(document=document, grantee=team.csi, user=team.student)
    second = services.grant_access(document=document, grantee=team.csi, user=team.student)
    assert first.pk == second.pk
    assert len(_events("DOCUMENT_GRANT_ADDED")) == 1


# --------------------------------------------------------------------------- re-wrap


def test_rewrap_skips_a_row_already_moved_to_another_domain(team: SimpleNamespace) -> None:
    """After waiting behind a change_level(), select_for_update(of=("self",)) re-reads
    the version row (new kek_id, new DEK) but keeps the OLD read of the joined
    document: the row must be skipped by its kek_id, not trusted by the level."""
    moved = _store(team, L.CONFIDENTIAL)
    normal = _store(team, L.CONFIDENTIAL)
    # The state such a row is read in: kek_id of the new domain, document still old.
    DocumentVersion.objects.filter(document=moved).update(kek_id="env:INTERNAL_ST:1")
    keys.new_version(KeyDomain.CONFIDENTIAL)
    assert services.rewrap_batch(KeyDomain.CONFIDENTIAL) == 1
    assert _v(normal).kek_id == "env:CONFIDENTIAL:2"
    assert _v(moved).kek_id == "env:INTERNAL_ST:1"  # untouched
    assert not _events("DOCUMENT_INTEGRITY_FAILED")


# --------------------------------------------------------------------------- MFA secrets


def test_mfa_secret_round_trip() -> None:
    sealed = services.encrypt_secret("JBSWY3DPEHPK3PXP")
    assert sealed.startswith("vault1:")
    assert "JBSWY3DPEHPK3PXP" not in sealed
    assert services.decrypt_secret(sealed) == "JBSWY3DPEHPK3PXP"
    assert services.encrypt_secret("JBSWY3DPEHPK3PXP") != sealed  # fresh nonce


@pytest.mark.parametrize("text", ["JBSWY3DPEHPK3PXP", "", "vault1:", "enc:abc"])
def test_clear_or_malformed_mfa_secret_is_refused(text: str) -> None:
    with pytest.raises(VaultIntegrityError):
        services.decrypt_secret(text)


def test_mfa_secret_keeps_its_kek_through_a_rotation(settings: Any) -> None:
    settings.VAULT_RETIRE_GRACE = timedelta(0)
    sealed = services.encrypt_secret("secret")
    keys.new_version(KeyDomain.MFA_SECRETS)
    assert services.decrypt_secret(sealed) == "secret"


def test_mfa_counter_blocks_retirement_while_a_secret_uses_the_version(settings: Any) -> None:
    from allauth.mfa.models import Authenticator

    from apps.vault.errors import KekRetirementError

    settings.VAULT_RETIRE_GRACE = timedelta(0)
    user = UserFactory.create(mfa=False)
    Authenticator.objects.create(
        user=user, type=Authenticator.Type.TOTP, data={"secret": services.encrypt_secret("s")}
    )
    keys.new_version(KeyDomain.MFA_SECRETS)
    with pytest.raises(KekRetirementError, match="referenced by 1"):
        keys.retire("env:MFA_SECRETS:1")
