"""The "Documents" card of the thesis dashboard: a link to the list and the number of
documents THIS user may read (role, level, explicit grant, module restrictions),
the very rule of the list itself; nothing decrypted, nothing more shown."""

from __future__ import annotations

import re
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.ui.tests.helpers import without_sidebar
from apps.vault import services
from apps.vault.models import AuditEvent

# Documents are stored (audited) then read by the views: one commit per service.
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def team() -> SimpleNamespace:
    thesis = ThesisFactory.create()

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(thesis=thesis, role=role, **kwargs).user

    team = SimpleNamespace(
        thesis=thesis,
        student=member(Role.PHD_STUDENT),
        director=member(Role.DIRECTOR),
        co_supervisor=member(Role.CO_SUPERVISOR),
        tutor=member(Role.INDUSTRIAL_TUTOR),
        csi=member(Role.CSI_MEMBER, active_until=timezone.now() + timedelta(days=60)),
    )
    team.documents = {
        level: services.store(
            thesis=thesis,
            upload=SimpleUploadedFile(f"{level}.pdf", b"%PDF-1.7\n%fictif\n%%EOF\n"),
            title=f"Document fictif {level.value}",
            level=level,
            user=team.student,
        )
        for level in L
    }
    return team


def _dashboard(team: SimpleNamespace, role: str) -> str:
    client = Client()
    client.force_login(getattr(team, role))
    response = client.get(reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk}))
    assert response.status_code == 200
    return response.content.decode()


def _card(html: str) -> str:
    card = re.search(r'<section class="block" data-block="documents">(.*?)</section>', html, re.S)
    assert card is not None, "no Documents card"
    return card.group(1)


def _count(html: str) -> int:
    match = re.search(r'data-documents="(\d+)"', _card(html))
    assert match is not None
    return int(match.group(1))


@pytest.mark.parametrize(
    ("role", "count"),
    [
        ("student", 4),  # every level
        ("director", 3),  # PUBLIC, INTERNAL_LIRMM, CONFIDENTIAL
        ("co_supervisor", 2),  # PUBLIC, INTERNAL_LIRMM
        ("tutor", 3),  # PUBLIC, INTERNAL_ST, CONFIDENTIAL
    ],
)
def test_the_card_counts_only_the_levels_the_user_may_read(
    team: SimpleNamespace, role: str, count: int
) -> None:
    assert _count(_dashboard(team, role)) == count


def test_an_explicit_grant_is_counted(team: SimpleNamespace) -> None:
    services.grant_access(
        document=team.documents[L.CONFIDENTIAL], grantee=team.co_supervisor, user=team.student
    )
    assert _count(_dashboard(team, "co_supervisor")) == 3


@pytest.mark.parametrize("role", ["student", "director", "co_supervisor", "tutor"])
def test_the_count_is_the_number_of_documents_on_the_list(team: SimpleNamespace, role: str) -> None:
    """Same rule as the list (vault.services.readable_documents), restrictions of other
    modules included: the card never promises a document the list does not show."""
    client = Client()
    client.force_login(getattr(team, role))
    listing = client.get(reverse("vault:documents", kwargs={"thesis_id": team.thesis.pk}))
    shown = len(re.findall(r'href="/t/[^"]+/documents/[0-9a-f-]{36}/"', listing.content.decode()))
    assert _count(_dashboard(team, role)) == shown


def test_the_card_holds_a_link_and_a_count_only(team: SimpleNamespace) -> None:
    card = _card(_dashboard(team, "co_supervisor"))
    url = reverse("vault:documents", kwargs={"thesis_id": team.thesis.pk})
    assert f'href="{url}"' in card
    assert "Document fictif" not in card  # no title
    assert "prochain module" not in card.lower()


def test_no_isolated_documents_link_outside_the_card(team: SimpleNamespace) -> None:
    # The sidebar (redesign) links to every module the role may open: not a card.
    html = without_sidebar(_dashboard(team, "director"))
    url = reverse("vault:documents", kwargs={"thesis_id": team.thesis.pk})
    assert html.count(f'href="{url}"') == 1
    assert f'href="{url}"' in _card(html)


def test_no_card_is_still_upcoming(team: SimpleNamespace) -> None:
    html = _dashboard(team, "director")
    upcoming = re.findall(
        r'<section class="block" data-block="([a-z_]+)">((?:(?!</section>).)*?prochain module)',
        html,
        re.S | re.I,
    )
    assert [key for key, _ in upcoming] == []  # the meetings card came with M5a


def test_counting_reads_and_audits_nothing(team: SimpleNamespace) -> None:
    client = Client()
    client.force_login(team.director)  # the sign-in itself is audited: before the count
    before = AuditEvent.objects.order_by("-seq").values_list("seq", flat=True).first()
    response = client.get(reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk}))
    assert response.status_code == 200
    new = list(AuditEvent.objects.filter(seq__gt=before).values_list("action", flat=True))
    assert new == [], new  # no DOCUMENT_READ: nothing was opened


def test_the_list_and_the_service_agree(team: SimpleNamespace) -> None:
    readable = services.readable_documents(team.tutor, team.thesis)
    assert {d.confidentiality for d in readable} == {"PUBLIC", "INTERNAL_ST", "CONFIDENTIAL"}
    assert services.readable_documents(team.csi, team.thesis) == []  # no DOCUMENT_READ
