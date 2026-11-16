"""Documents (frontend step 2, point 11): a real, styled table, and titles that
repeat numbered (« Justificatif 2026-07-14 (2) ») without revealing anything.

The numbers are counted among the documents THIS viewer may read (the list's own
rule, vault.services.readable_documents), in the order they were stored: a
« (2) » never tells that a hidden document has the same title. Only a number is
added: never a category, a level or a date of the hidden part.
"""

from __future__ import annotations

import html as html_module
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from apps.accounts.permissions import can_access_document
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault import services

pytestmark = pytest.mark.django_db(transaction=True)

TITLE = "Justificatif 2026-07-14"


@pytest.fixture
def team() -> SimpleNamespace:
    thesis = ThesisFactory.create()

    def member(role: Role) -> Any:
        return MembershipFactory.create(thesis=thesis, role=role).user

    return SimpleNamespace(
        thesis=thesis,
        student=member(Role.PHD_STUDENT),
        director=member(Role.DIRECTOR),
        co_supervisor=member(Role.CO_SUPERVISOR),
        tutor=member(Role.INDUSTRIAL_TUTOR),
    )


def _store(team: SimpleNamespace, title: str, level: L) -> Any:
    return services.store(
        thesis=team.thesis,
        upload=SimpleUploadedFile("piece.pdf", b"%PDF-1.7\n%fictif\n%%EOF\n"),
        title=title,
        level=level,
        user=team.student,
    )


def _get(team: SimpleNamespace, role: str, url: str) -> str:
    client = Client()
    client.force_login(getattr(team, role))
    response = client.get(url)
    assert response.status_code == 200
    return html_module.unescape(response.content.decode())


def _list(team: SimpleNamespace, role: str) -> str:
    return _get(team, role, reverse("vault:documents", kwargs={"thesis_id": team.thesis.pk}))


def _titles(page: str) -> list[str]:
    table = re.search(r'<table class="table documents">.*?</table>', page, re.S)
    assert table is not None
    return [
        t.strip()
        for t in re.findall(r'<td class="document-title"><a [^>]*>(.*?)</a>', table.group(0))
    ]


def test_a_real_styled_table(team: SimpleNamespace) -> None:
    _store(team, "Convention fictive", L.PUBLIC)
    page = _list(team, "student")
    assert re.search(r'<div class="table-wrap">\s*<table class="table documents">', page)
    headers = re.findall(r'<th scope="col"[^>]*>', page)
    assert len(headers) == 4
    # The level is a badge: a word with an icon, never a colour alone.
    assert re.search(r'<span class="badge badge-\w+">.*?Public</span>', page, re.S)


def test_repeated_titles_are_numbered_in_storing_order(team: SimpleNamespace) -> None:
    first = _store(team, TITLE, L.PUBLIC)
    second = _store(team, TITLE, L.PUBLIC)
    third = _store(team, TITLE, L.PUBLIC)
    _store(team, "Autre document fictif", L.PUBLIC)
    page = _list(team, "student")
    assert sorted(t for t in _titles(page) if t.startswith("Justificatif")) == [
        TITLE,
        f"{TITLE} (2)",
        f"{TITLE} (3)",
    ]
    assert "Autre document fictif" in _titles(page)
    # Each detail page carries its own label, the one of the list.
    for document, label in ((first, TITLE), (second, f"{TITLE} (2)"), (third, f"{TITLE} (3)")):
        url = reverse(
            "vault:document", kwargs={"thesis_id": team.thesis.pk, "document_id": document.pk}
        )
        detail = _get(team, "student", url)
        assert re.search(rf"<h1>\s*{re.escape(label)}\s*</h1>", detail), label


def test_numbers_count_what_the_viewer_reads_only(team: SimpleNamespace) -> None:
    hidden = _store(team, TITLE, L.INTERNAL_ST)  # stored first
    shown = _store(team, TITLE, L.INTERNAL_LIRMM)
    assert not can_access_document(team.co_supervisor, hidden)
    assert can_access_document(team.co_supervisor, shown)
    co_sup = _list(team, "co_supervisor")
    assert _titles(co_sup) == [TITLE]  # not « (2) »: nothing tells the hidden one
    student = _list(team, "student")
    assert sorted(_titles(student)) == [TITLE, f"{TITLE} (2)"]


def test_a_title_that_already_ends_with_a_number_is_not_repeated(team: SimpleNamespace) -> None:
    _store(team, f"{TITLE} (2)", L.PUBLIC)
    _store(team, TITLE, L.PUBLIC)
    _store(team, TITLE, L.PUBLIC)
    titles = _titles(_list(team, "student"))
    assert len(titles) == len(set(titles)) == 3
