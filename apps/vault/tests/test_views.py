"""Document pages: list, detail, upload, new version, download, level change, grants.

transaction=True everywhere (CLAUDE.md): signing in is audited, then a vault
operation follows; each request commits its own transaction, as in production.
"""

import re
import secrets
import sys
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.views.debug import ExceptionReporter

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.accounts.tests.helpers import mfa_login, password_reauthenticate
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault import services, views
from apps.vault.models import AuditEvent, Document, DocumentAccessGrant

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = secrets.token_urlsafe(16)
ROLES = ["student", "director", "co_supervisor", "tutor", "csi", "guest"]


def _pdf(marker: str = "fictif") -> bytes:
    return f"%PDF-1.7\n%Compte rendu {marker}\n%%EOF\n".encode()


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
        csi=member(Role.CSI_MEMBER, active_until=timezone.now() + timedelta(days=60)),
        guest=member(Role.GUEST),
    )


def _store(team: SimpleNamespace, level: L, title: str = "Compte rendu fictif", **kw: Any) -> Any:
    data = kw.pop("data", _pdf())
    return services.store(
        thesis=team.thesis,
        upload=SimpleUploadedFile("rapport.pdf", data),
        title=title,
        level=level,
        user=team.student,
    )


def _client(user: Any) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _url(name: str, thesis: Any, **kwargs: Any) -> str:
    return reverse(f"vault:{name}", kwargs={"thesis_id": thesis.pk, **kwargs})


def _download(document: Any) -> str:
    version = document.versions.get(version=document.current_version)
    return _url("download", document.thesis, version_id=version.pk)


def _denied(operation: str) -> list[AuditEvent]:
    return [
        e
        for e in AuditEvent.objects.filter(action="DOCUMENT_ACCESS_DENIED")
        if e.details_json.get("operation") == operation
    ]


# --------------------------------------------------------------------------- list


def test_list_shows_only_readable_documents(team: SimpleNamespace) -> None:
    titles = {level: f"Document {level.value.lower()}" for level in L}
    for level, title in titles.items():
        _store(team, level, title=title)
    expected = {
        "student": set(L),
        "director": {L.PUBLIC, L.INTERNAL_LIRMM, L.CONFIDENTIAL},
        "co_supervisor": {L.PUBLIC, L.INTERNAL_LIRMM},
        "tutor": {L.PUBLIC, L.INTERNAL_ST, L.CONFIDENTIAL},
        "csi": set(),
    }
    # A GUEST has no thesis-level right (M1): 403 before any list.
    assert _client(team.guest).get(_url("documents", team.thesis)).status_code == 403
    for role, levels in expected.items():
        page = _client(getattr(team, role)).get(_url("documents", team.thesis))
        assert page.status_code == 200, role
        shown = {level for level, title in titles.items() if title in page.content.decode()}
        assert shown == levels, role


def test_list_refuses_outsiders_and_redirects_anonymous(team: SimpleNamespace) -> None:
    assert _client(UserFactory.create()).get(_url("documents", team.thesis)).status_code == 403
    response = Client().get(_url("documents", team.thesis))
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


# --------------------------------------------------------------------------- detail


def test_detail_allowed_and_refused(team: SimpleNamespace) -> None:
    document = _store(team, L.INTERNAL_ST)
    url = _url("document", team.thesis, document_id=document.pk)
    assert _client(team.tutor).get(url).status_code == 200
    assert _client(team.director).get(url).status_code == 403
    assert len(_denied("detail")) == 1


def test_detail_of_another_thesis_through_my_url_is_refused(team: SimpleNamespace) -> None:
    other = ThesisFactory.create()
    MembershipFactory.create(thesis=other, role=Role.PHD_STUDENT, user=team.student)
    foreign = services.store(
        thesis=other,
        upload=SimpleUploadedFile("a.pdf", _pdf()),
        title="Autre",
        level=L.PUBLIC,
        user=team.student,
    )
    url = _url("document", team.thesis, document_id=foreign.pk)
    assert _client(team.student).get(url).status_code == 403
    assert len(_denied("detail")) == 1


# --------------------------------------------------------------------------- upload


def test_upload_form_warns_that_titles_are_not_encrypted(team: SimpleNamespace) -> None:
    page = _client(team.student).get(_url("upload", team.thesis))
    assert page.status_code == 200
    html = page.content.decode().replace("&#x27;", "'")
    assert "Le titre n'est pas chiffré\u00a0: n'y mettez aucune information confidentielle." in html


def test_student_uploads_a_document(team: SimpleNamespace) -> None:
    response = _client(team.student).post(
        _url("upload", team.thesis),
        {
            "title": "Rapport fictif",
            "level": L.INTERNAL_LIRMM.value,
            "file": SimpleUploadedFile("rapport.pdf", _pdf()),
        },
    )
    document = Document.objects.get()
    assert response.status_code == 302
    assert response["Location"] == _url("document", team.thesis, document_id=document.pk)
    assert document.confidentiality == L.INTERNAL_LIRMM


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor", "csi", "guest"])
def test_only_the_student_uploads(team: SimpleNamespace, role: str) -> None:
    client = _client(getattr(team, role))
    assert client.get(_url("upload", team.thesis)).status_code == 403
    response = client.post(
        _url("upload", team.thesis),
        {"title": "x", "level": "PUBLIC", "file": SimpleUploadedFile("a.pdf", _pdf())},
    )
    assert response.status_code == 403
    assert not Document.objects.exists()


def test_upload_too_large_is_a_form_error(team: SimpleNamespace) -> None:
    big = b"%PDF-1.7\n" + b"a" * (26 * 1024 * 1024)
    response = _client(team.student).post(
        _url("upload", team.thesis),
        {"title": "Gros", "level": "PUBLIC", "file": SimpleUploadedFile("gros.pdf", big)},
    )
    assert response.status_code == 200
    assert "25 Mo" in response.content.decode()
    assert not Document.objects.exists()


def test_upload_with_a_spoofed_type_is_a_form_error(team: SimpleNamespace) -> None:
    response = _client(team.student).post(
        _url("upload", team.thesis),
        {
            "title": "Faux",
            "level": "PUBLIC",
            "file": SimpleUploadedFile("image.png", _pdf(), content_type="image/png"),
        },
    )
    assert response.status_code == 200
    assert not Document.objects.exists()


def test_new_version(team: SimpleNamespace) -> None:
    document = _store(team, L.PUBLIC)
    url = _url("new_version", team.thesis, document_id=document.pk)
    file = SimpleUploadedFile("v2.pdf", _pdf("v2"))
    assert _client(team.director).post(url, {"file": file}).status_code == 403
    file = SimpleUploadedFile("v2.pdf", _pdf("v2"))
    assert _client(team.student).post(url, {"file": file}).status_code == 302
    document.refresh_from_db()
    assert document.current_version == 2


# --------------------------------------------------------------------------- download


@pytest.mark.parametrize("level", [L.PUBLIC, L.INTERNAL_LIRMM, L.INTERNAL_ST])
def test_download_follows_the_access_rules(team: SimpleNamespace, level: L) -> None:
    document = _store(team, level)
    readers = {
        L.PUBLIC: {"student", "director", "co_supervisor", "tutor"},
        L.INTERNAL_LIRMM: {"student", "director", "co_supervisor"},
        L.INTERNAL_ST: {"student", "tutor"},
    }[level]
    for role in ROLES:
        response = _client(getattr(team, role)).get(_download(document))
        expected = 200 if role in readers else 403
        assert response.status_code == expected, (role, level)
        if expected == 200:
            assert response.content == _pdf()
    # The GUEST is refused by the thesis guard (M1, no thesis-level right), before
    # the document service: only refusals by level are document-access events.
    assert len(_denied("read")) == len(ROLES) - len(readers) - 1


def test_download_headers(team: SimpleNamespace) -> None:
    response = _client(team.student).get(_download(_store(team, L.PUBLIC)))
    assert response["Content-Type"] == "application/octet-stream"
    assert response["Content-Disposition"] == 'attachment; filename="compte-rendu-fictif.pdf"'
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "no-store" in response["Cache-Control"]
    assert "sandbox" in response["Content-Security-Policy"]
    assert response["Referrer-Policy"] == "no-referrer"


def test_version_of_another_thesis_through_my_url_is_refused(team: SimpleNamespace) -> None:
    """IDOR: the student also belongs to another thesis and can read that version
    there, but not through this thesis' URL."""
    other = ThesisFactory.create()
    MembershipFactory.create(thesis=other, role=Role.PHD_STUDENT, user=team.student)
    foreign = services.store(
        thesis=other,
        upload=SimpleUploadedFile("a.pdf", _pdf()),
        title="Autre",
        level=L.PUBLIC,
        user=team.student,
    )
    version = foreign.versions.get()
    client = _client(team.student)
    assert client.get(_url("download", team.thesis, version_id=version.pk)).status_code == 403
    assert client.get(_url("download", other, version_id=version.pk)).status_code == 200
    (denied,) = _denied("read")
    assert denied.thesis == team.thesis


def test_unknown_version_is_a_plain_403(team: SimpleNamespace) -> None:
    import uuid

    response = _client(team.student).get(_url("download", team.thesis, version_id=uuid.uuid4()))
    assert response.status_code == 403


def test_confidential_download_needs_a_recent_second_factor(team: SimpleNamespace) -> None:
    url = _download(_store(team, L.CONFIDENTIAL))
    client = _client(team.student)  # signed in without a recent factor
    response = client.get(url)
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("mfa_reauthenticate"))
    htmx = client.get(url, HTTP_HX_REQUEST="true")
    assert htmx.status_code == 200
    assert htmx["HX-Redirect"].startswith(reverse("mfa_reauthenticate"))
    assert not AuditEvent.objects.filter(action="DOCUMENT_READ").exists()


def test_password_alone_is_not_enough_for_a_confidential_download(team: SimpleNamespace) -> None:
    url = _download(_store(team, L.CONFIDENTIAL))
    client = _client(team.student)
    password_reauthenticate(client, PASSWORD)
    response = client.get(url)
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("mfa_reauthenticate"))


def test_confidential_download_after_signing_in_with_the_factor(team: SimpleNamespace) -> None:
    url = _download(_store(team, L.CONFIDENTIAL))
    client = Client()
    mfa_login(client, team.director, PASSWORD)
    assert client.get(url).status_code == 200


def test_refusal_comes_before_the_reauthentication(team: SimpleNamespace) -> None:
    """A role without access gets 403, never an invitation to re-authenticate."""
    url = _download(_store(team, L.CONFIDENTIAL))
    assert _client(team.co_supervisor).get(url).status_code == 403


def test_integrity_failure_is_a_page_without_details(team: SimpleNamespace) -> None:
    from apps.vault import storage

    document = _store(team, L.PUBLIC)
    path = storage.root() / document.versions.get().ciphertext_path
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 1
    path.write_bytes(bytes(raw))
    response = _client(team.student).get(_download(document))
    assert response.status_code == 503
    assert "journalisé" in response.content.decode()
    # The SRI attribute of htmx (SR-11) is on every page: removed, then the word is
    # still absent from everything else.
    page, sri = re.subn(r'\sintegrity="sha384-[A-Za-z0-9+/=]+"', "", response.content.decode())
    assert sri == 1
    assert "integrity" not in page.lower()
    assert AuditEvent.objects.filter(action="DOCUMENT_INTEGRITY_FAILED").exists()


def test_download_view_hides_the_plaintext_in_error_reports(
    team: SimpleNamespace, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point 6: an exception raised in the view AFTER open_version() returned the
    plaintext must not expose it in the error report (DEBUG=True and False)."""
    marker = "TEMOIN-" + secrets.token_hex(8)  # not in any source line
    document = _store(team, L.PUBLIC, data=_pdf(marker))

    def broken(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("failure after open_version()")

    monkeypatch.setattr(views, "content_disposition_header", broken)
    for debug in (True, False):
        settings.DEBUG = debug
        client = _client(team.student)
        try:
            client.get(_download(document))
        except RuntimeError:
            exc_info = sys.exc_info()
        else:
            raise AssertionError("the view should have raised")
        reporter = ExceptionReporter(None, *exc_info)
        frames = [f for f in reporter.get_traceback_frames() if "/tests/" not in f["filename"]]
        assert any(f["filename"].endswith("vault/views.py") for f in frames)
        for frame in frames:  # this test's own frame holds the marker: not inspected
            shown = repr(frame["vars"]) + repr(frame.get("context_line", ""))
            assert marker not in shown, (debug, frame["filename"], frame["function"])
        # The view's own frame is masked by @sensitive_variables (defence in depth:
        # Opened and HttpResponse do not show their content in repr() either).
        view_frames = [f for f in frames if f["filename"].endswith("vault/views.py")]
        substitute = reporter.filter.cleansed_substitute
        assert view_frames
        assert all(substitute in value for f in view_frames for _, value in f["vars"]), debug


# --------------------------------------------------------------------------- level and grants


def test_change_level_needs_the_second_factor_and_the_right(team: SimpleNamespace) -> None:
    document = _store(team, L.CONFIDENTIAL)
    url = _url("change_level", team.thesis, document_id=document.pk)
    assert _client(team.director).post(url, {"level": "PUBLIC"}).status_code == 403
    assert _client(team.co_supervisor).post(url, {"level": "PUBLIC"}).status_code == 403
    response = _client(team.student).post(url, {"level": "PUBLIC"})
    assert response["Location"].startswith(reverse("mfa_reauthenticate"))
    client = Client()
    mfa_login(client, team.student, PASSWORD)
    assert client.post(url, {"level": "PUBLIC"}).status_code == 302
    document.refresh_from_db()
    assert document.confidentiality == L.PUBLIC


def test_explicit_grant_through_the_pages(team: SimpleNamespace) -> None:
    document = _store(team, L.CONFIDENTIAL)
    grant_url = _url("grant", team.thesis, document_id=document.pk)
    for role in ("director", "co_supervisor", "tutor"):
        assert (
            _client(getattr(team, role)).post(grant_url, {"grantee": team.csi.pk}).status_code
            == 403
        )
    student = Client()
    mfa_login(student, team.student, PASSWORD)
    assert student.post(grant_url, {"grantee": team.csi.pk}).status_code == 302
    csi = Client()
    mfa_login(csi, team.csi, PASSWORD)
    assert csi.get(_download(document)).status_code == 200
    grant = DocumentAccessGrant.objects.get()
    revoke_url = _url("revoke_grant", team.thesis, document_id=document.pk, grant_id=grant.pk)
    assert student.post(revoke_url).status_code == 302
    assert csi.get(_download(document)).status_code == 403
