"""The pages (M13b, criterion 14 and the views): the student's page with its
warning and history, the download, the tutor's page; refusals per role."""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace
from typing import Any

import pytest

from apps.export import services as export
from apps.export.models import StExportConsent
from apps.export.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)
NOT_TUTOR = ["student", "director", "co_supervisor", "st_co_supervisor", "csi", "guest"]


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def test_the_student_s_page_warns_and_shows_the_history(team: SimpleNamespace) -> None:
    b"".join(export.start_export(thesis=team.thesis, user=team.student, recent_mfa=True,
                                 all_versions=True).chunks())  # fmt: skip
    body = h.client_for(team.student).get(h.url(team, "page")).content.decode()
    assert "en clair" in body and "FileVault" in body
    assert "Complet" in body and "toutes les versions" in body


def test_an_interrupted_export_shows_in_the_history(team: SimpleNamespace) -> None:
    chunks = export.start_export(thesis=team.thesis, user=team.student,
                                 recent_mfa=True).chunks()  # fmt: skip
    next(chunks)
    chunks.close()
    body = h.client_for(team.student).get(h.url(team, "page")).content.decode()
    assert "Interrompu" in body


def test_the_download(team: SimpleNamespace) -> None:
    client = h.mfa_browser(team.student)
    response = h.csrf_post(client, team, h.url(team, "download"), {"all_versions": "on"})
    assert response.status_code == 200 and response.streaming
    assert response["Content-Type"] == "application/zip"
    assert response["Content-Disposition"].startswith('attachment; filename="export-these-')
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "no-store" in response["Cache-Control"]
    archive = zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content)))
    assert h.read(archive, "manifest.json")["options"]["all_versions"] is True


def test_the_tutor_s_page_and_agreement(team: SimpleNamespace) -> None:
    client = h.mfa_browser(team.tutor)
    assert client.get(h.url(team, "st")).status_code == 200
    response = h.csrf_post(client, team, h.url(team, "st_grant"), {})
    assert response.status_code == 302
    consent = StExportConsent.objects.get()
    response = h.csrf_post(client, team, h.url(team, "st_revoke", consent_id=consent.pk), {})
    assert response.status_code == 302
    consent.refresh_from_db()
    assert consent.revoked_at is not None


def test_the_tutor_sets_the_end_of_st_confidentiality(team: SimpleNamespace) -> None:
    from apps.accounts.models import Thesis

    client = h.mfa_browser(team.tutor)
    response = h.csrf_post(client, team, h.url(team, "st_date"), {"until": "2030-01-01"})
    assert response.status_code == 302
    assert str(Thesis.objects.get(pk=team.thesis.pk).st_confidential_until) == "2030-01-01"


@pytest.mark.parametrize("attr", NOT_TUTOR)
def test_the_tutor_s_page_is_refused_to_the_others(team: SimpleNamespace, attr: str) -> None:
    client = h.mfa_browser(getattr(team, attr))
    assert client.get(h.url(team, "st")).status_code == 403
    assert h.csrf_post(client, team, h.url(team, "st_grant"), {}).status_code == 403
    assert StExportConsent.objects.count() == 0


def test_the_student_sees_the_agreement_on_the_page(team: SimpleNamespace) -> None:
    export.grant_st_consent(thesis=team.thesis, user=team.tutor, recent_mfa=True)
    body = h.client_for(team.student).get(h.url(team, "page")).content.decode()
    assert "Accord ST" in body
