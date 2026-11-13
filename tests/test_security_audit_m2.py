"""M2 security audit: one test (or a pointer to one) per lead of the end-of-module review.

Leads already closed elsewhere:
- 2 IDOR: apps/vault/tests/test_views.py::test_version_of_another_thesis_through_my_url_is_refused,
  ::test_detail_of_another_thesis_through_my_url_is_refused
- 8 audit_verify on an altered database: apps/vault/tests/test_audit_verify.py
- 9 CONFIDENTIAL download and re-authentication: apps/vault/tests/test_views.py
  ::test_confidential_download_needs_a_recent_second_factor,
  ::test_password_alone_is_not_enough_for_a_confidential_download
- 10 concurrency: apps/vault/tests/test_services.py
  ::test_concurrent_level_change_never_causes_a_false_integrity_failure;
  apps/vault/tests/test_keys.py::test_rotation_waits_for_an_uncommitted_wrap_then_refuses_to_retire,
  ::test_retire_waits_for_transactions_holding_the_shared_lock
"""

import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest
from allauth.mfa.models import Authenticator
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory
from apps.accounts.tests.helpers import mfa_login
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault import crypto, services, storage
from apps.vault.models import AuditEvent, DocumentVersion

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = secrets.token_urlsafe(16)
T0 = datetime(2026, 12, 1, 10, 0, tzinfo=UTC)


def _marker() -> str:
    return "TEMOIN-" + secrets.token_hex(8)


def _pdf(marker: str) -> bytes:
    return f"%PDF-1.7\n%{marker} contenu fictif {marker}\n%%EOF\n".encode()


@pytest.fixture
def team() -> SimpleNamespace:
    thesis = ThesisFactory.create()

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(
            thesis=thesis, role=role, user__password=PASSWORD, **kwargs
        ).user

    return SimpleNamespace(
        thesis=thesis,
        student=member(Role.PHD_STUDENT),
        director=member(Role.DIRECTOR),
        co_supervisor=member(Role.CO_SUPERVISOR),
        tutor=member(Role.INDUSTRIAL_TUTOR),
    )


def _store(team: SimpleNamespace, level: L, marker: str, title: str = "Fictif") -> Any:
    return services.store(
        thesis=team.thesis,
        upload=SimpleUploadedFile("r.pdf", _pdf(marker)),
        title=title,
        level=level,
        user=team.student,
    )


# --------------------------------------------------------------------------- 1. no clear on disk


def test_no_byte_of_the_plaintext_anywhere_in_the_storage(team: SimpleNamespace) -> None:
    first, second = _marker(), _marker()
    document = _store(team, L.CONFIDENTIAL, first)
    services.add_version(
        document=document, upload=SimpleUploadedFile("v2.pdf", _pdf(second)), user=team.student
    )
    services.change_level(document=document, level=L.INTERNAL_ST, user=team.student)
    files = [p for p in storage.root().rglob("*") if p.is_file()]
    assert len(files) == 4  # 2 versions, before and after the level change
    for path in files:
        raw = path.read_bytes()
        for marker in (first, second):
            assert marker.encode() not in raw, path
        assert b"%PDF" not in raw and b"contenu fictif" not in raw, path


# --------------------------------------------------------------------------- 3. rights


@pytest.mark.parametrize("role", ["director", "co_supervisor"])
def test_supervisors_cannot_store_change_levels_or_grant(team: SimpleNamespace, role: str) -> None:
    user = getattr(team, role)
    document = _store(team, L.CONFIDENTIAL, _marker())
    with pytest.raises(PermissionDenied):
        services.store(
            thesis=team.thesis,
            upload=SimpleUploadedFile("a.pdf", _pdf("x")),
            title="x",
            level=L.PUBLIC,
            user=user,
        )
    with pytest.raises(PermissionDenied):
        services.change_level(document=document, level=L.PUBLIC, user=user)
    with pytest.raises(PermissionDenied):
        services.grant_access(document=document, grantee=team.tutor, user=user)
    operations = {
        e.details_json["operation"]
        for e in AuditEvent.objects.filter(action="DOCUMENT_ACCESS_DENIED", actor=user)
    }
    assert operations == {"store", "change_level", "grant"}


def test_tutor_does_not_read_internal_lirmm_nor_co_supervisor_internal_st(
    team: SimpleNamespace,
) -> None:
    lirmm = _store(team, L.INTERNAL_LIRMM, _marker()).versions.get().pk
    st = _store(team, L.INTERNAL_ST, _marker()).versions.get().pk
    with pytest.raises(PermissionDenied):
        services.open_version(lirmm, team.tutor)
    with pytest.raises(PermissionDenied):
        services.open_version(st, team.co_supervisor)
    assert services.open_version(lirmm, team.co_supervisor).data.startswith(b"%PDF")
    assert services.open_version(st, team.tutor).data.startswith(b"%PDF")


# --------------------------------------------------------------------------- 4. CSI grant


def test_csi_grant_falls_with_the_membership_and_with_the_level(
    team: SimpleNamespace, time_machine: Any
) -> None:
    time_machine.move_to(T0, tick=False)
    csi = MembershipFactory.create(
        thesis=team.thesis, role=Role.CSI_MEMBER, active_until=T0 + timedelta(days=60)
    ).user
    document = _store(team, L.CONFIDENTIAL, _marker())
    version = document.versions.get().pk
    services.grant_access(document=document, grantee=csi, user=team.student)
    assert services.open_version(version, csi, recent_mfa=True).data.startswith(b"%PDF")
    time_machine.move_to(T0 + timedelta(days=61), tick=False)  # membership expired
    with pytest.raises(PermissionDenied):
        services.open_version(version, csi, recent_mfa=True)
    # Leaving CONFIDENTIAL revokes the grants (and a CSI member has no level right).
    time_machine.move_to(T0 + timedelta(days=1), tick=False)
    services.change_level(document=document, level=L.INTERNAL_LIRMM, user=team.student)
    with pytest.raises(PermissionDenied):
        services.open_version(version, csi, recent_mfa=True)


# --------------------------------------------------------------------------- 5. leaks


def test_no_dek_wrapped_dek_plaintext_or_title_in_logs_or_audit_log(
    team: SimpleNamespace, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    deks: list[bytes] = []
    real_new_dek = crypto.new_dek

    def recording_new_dek() -> bytes:
        deks.append(real_new_dek())
        return deks[-1]

    monkeypatch.setattr(crypto, "new_dek", recording_new_dek)
    marker, title = _marker(), "Titre " + _marker()
    with caplog.at_level(logging.DEBUG):
        document = _store(team, L.CONFIDENTIAL, marker, title=title)
        version = document.versions.get().pk
        services.open_version(version, team.student, recent_mfa=True)
        services.change_level(document=document, level=L.INTERNAL_ST, user=team.student)
        call_command("rotate_kek", "INTERNAL_ST", stdout=StringIO())
        services.decrypt_secret(services.encrypt_secret("JBSWY3DPEHPK3PXP"))
    wrapped = [bytes(v) for v in DocumentVersion.objects.values_list("wrapped_dek", flat=True)]
    audit_rows = str(list(AuditEvent.objects.values()))
    forbidden = [marker, title, "JBSWY3DPEHPK3PXP"] + [d.hex() for d in deks + wrapped]
    forbidden += [repr(d) for d in deks + wrapped]
    assert len(deks) == 2
    for text in (caplog.text, audit_rows):
        for leak in forbidden:
            assert leak not in text


def test_sql_queries_are_never_logged_with_their_parameters() -> None:
    """Queries carry wrapped DEKs and titles: django.db.backends is pinned at INFO."""
    assert logging.getLogger("django.db.backends").getEffectiveLevel() >= logging.INFO
    assert logging.getLogger("django.db.backends").level == logging.INFO


@pytest.mark.parametrize(
    "title",
    [
        "Compte\r\nSet-Cookie: x=1",
        'Rapport "final"; filename=evil.exe',
        "../../etc/passwd",
        "Thèse — état de l'art ✓ 😀",
        chr(0x202E) + "gnp.exe",  # right-to-left override
    ],
)
def test_download_name_is_a_safe_header(team: SimpleNamespace, title: str) -> None:
    document = _store(team, L.PUBLIC, _marker(), title=title)
    client = Client()
    client.force_login(team.student)
    url = reverse(
        "vault:download",
        kwargs={"thesis_id": team.thesis.pk, "version_id": document.versions.get().pk},
    )
    header = client.get(url)["Content-Disposition"]
    assert re.fullmatch(r'attachment; filename="[a-z0-9-]{1,80}\.pdf"', header), header


# --------------------------------------------------------------------------- 6. polyglot


def test_pdf_html_polyglot_is_served_inert(team: SimpleNamespace) -> None:
    polyglot = b"%PDF-1.7\n<html><body><script>alert(document.cookie)</script></body></html>\n"
    document = services.store(
        thesis=team.thesis,
        upload=SimpleUploadedFile("p.pdf", polyglot),
        title="Polyglotte",
        level=L.PUBLIC,
        user=team.student,
    )
    client = Client()
    client.force_login(team.student)
    response = client.get(
        reverse(
            "vault:download",
            kwargs={"thesis_id": team.thesis.pk, "version_id": document.versions.get().pk},
        )
    )
    assert response.status_code == 200
    assert response["Content-Type"] == "application/octet-stream"
    assert response["Content-Disposition"].startswith("attachment;")
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in response["Content-Security-Policy"]


# --------------------------------------------------------------------------- 7. rotation end to end


def test_rotation_end_to_end_keeps_documents_and_totp(
    team: SimpleNamespace, time_machine: Any
) -> None:
    time_machine.move_to(T0, tick=False)
    documents = {level: _store(team, level, _marker()) for level in L}
    for domain in ("PUBLIC", "INTERNAL_LIRMM", "INTERNAL_ST", "CONFIDENTIAL", "MFA_SECRETS"):
        call_command("rotate_kek", domain, stdout=StringIO())
    time_machine.move_to(T0 + timedelta(minutes=11), tick=False)
    for domain in ("PUBLIC", "INTERNAL_LIRMM", "INTERNAL_ST", "CONFIDENTIAL", "MFA_SECRETS"):
        out = StringIO()
        call_command("rotate_kek", domain, "--retire", stdout=out)
        assert f"retired env:{domain}:1" in out.getvalue()
    for level, document in documents.items():
        version = document.versions.get()
        assert version.kek_id == f"env:{level.value}:2"
        opened = services.open_version(version.pk, team.student, recent_mfa=True)
        assert opened.data.startswith(b"%PDF")
    totp = Authenticator.objects.get(user=team.director, type=Authenticator.Type.TOTP)
    from apps.vault import formats

    assert formats.unpack_secret(totp.data["secret"])[0] == "env:MFA_SECRETS:2"
    time_machine.move_to(T0 + timedelta(minutes=12), tick=False)
    mfa_login(Client(), team.director, PASSWORD)  # the TOTP still signs in
