"""Dashboard cards, step 8b: what no tile shows, from read-only service functions
filtered per viewer (each one: allowed for the roles holding its action, refused
to every other role).

- Administrative file: the 3 next items THIS viewer sees (the tutor, the CIFRE
  part): overdue to deposit first, then by due date; « en attente de validation
  depuis X jours » for a deposit. Cut after the filter.
- Conferences: the submissions by status.
- Documents: the 3 latest documents the viewer reads, numbered like the list,
  with the level badge.
- Team: the members by role, and the end of the CSI when it is near.
None of them opens a sealed text (test_dashboard_cards.py counts the reads).
"""

from __future__ import annotations

import datetime as dt
import html as html_module
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.permissions import ROLE_ACTIONS, Action
from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.travel.tests.helpers import NOW, D, client_for, make_team

pytestmark = pytest.mark.django_db(transaction=True)

ROLE_OF = {
    Role.PHD_STUDENT: "student",
    Role.DIRECTOR: "director",
    Role.CO_SUPERVISOR: "co_supervisor",
    Role.INDUSTRIAL_TUTOR: "tutor",
    Role.CSI_MEMBER: "csi",
    Role.GUEST: "guest",
}


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=False)  # 2027-01-20


@pytest.fixture
def team() -> SimpleNamespace:
    from apps.admin_file import services as admin_file

    team = make_team()
    admin_file.seed_items(team.thesis)
    return team


def _functions() -> dict[str, tuple[Any, Action]]:
    from apps.accounts.services import team_summary
    from apps.admin_file.services import next_items
    from apps.conferences.services import submission_counts
    from apps.vault.services import latest_documents

    return {
        "next_items": (next_items, Action.CIFRE_FILE_READ),
        "submission_counts": (submission_counts, Action.SUBMISSION_READ),
        "latest_documents": (latest_documents, Action.DOCUMENT_READ),
        "team_summary": (team_summary, Action.VIEW_TEAM),
    }


@pytest.mark.parametrize(
    "name", ["next_items", "submission_counts", "latest_documents", "team_summary"]
)
@pytest.mark.parametrize("role", list(Role))
def test_allowed_or_refused_per_role(team: SimpleNamespace, name: str, role: Role) -> None:
    function, action = _functions()[name]
    user = getattr(team, ROLE_OF[role])
    if action in ROLE_ACTIONS[role]:
        function(user, team.thesis)  # no exception
    else:
        with pytest.raises(PermissionDenied):
            function(user, team.thesis)


# --------------------------------------------------------------------------- file


def _items(team: SimpleNamespace, role: str) -> list[Any]:
    from apps.admin_file.services import visible_items

    return list(visible_items(getattr(team, role), team.thesis))


def test_next_items_cut_after_the_filter(team: SimpleNamespace) -> None:
    """Items hidden from the tutor are nearer than theirs: the tutor still gets
    three of their own part."""
    from apps.admin_file.models import RequiredItem
    from apps.admin_file.services import next_items

    tutor_ids = {i.pk for i in _items(team, "tutor")}
    hidden = [i for i in _items(team, "student") if i.pk not in tutor_ids]
    shown = [i for i in _items(team, "student") if i.pk in tutor_ids]
    assert len(hidden) >= 3 and len(shown) >= 3
    for n, item in enumerate(hidden):
        RequiredItem.objects.filter(pk=item.pk).update(due_date=D(2027, 1, 1) + dt.timedelta(n))
    for n, item in enumerate(shown):
        RequiredItem.objects.filter(pk=item.pk).update(due_date=D(2027, 6, 1) + dt.timedelta(n))
    tutor = next_items(team.tutor, team.thesis)
    assert len(tutor) == 3 and {n.item.pk for n in tutor} <= tutor_ids
    student = next_items(team.student, team.thesis)
    assert [n.item.pk for n in student] == [i.pk for i in hidden[:3]]
    assert all(n.overdue for n in student)


def test_a_deposit_waits_and_comes_after_the_overdue(team: SimpleNamespace) -> None:
    from apps.admin_file.models import ItemStatus, RequiredItem
    from apps.admin_file.services import next_items

    items = _items(team, "student")
    RequiredItem.objects.filter(thesis=team.thesis).update(due_date=D(2027, 9, 1))
    RequiredItem.objects.filter(pk=items[0].pk).update(due_date=D(2027, 1, 10))  # overdue
    RequiredItem.objects.filter(pk=items[1].pk).update(
        due_date=D(2027, 1, 5),
        status=ItemStatus.SUBMITTED,
        cycle=1,
        submitted_by=team.student,
        submitted_version=None,
        attachment_required=False,
        submitted_at=dt.datetime(2027, 1, 16, 9, 0, tzinfo=dt.UTC),
    )
    first, second, _third = next_items(team.student, team.thesis)
    assert (first.item.pk, first.overdue) == (items[0].pk, True)
    assert (second.item.pk, second.overdue, second.waiting_days) == (items[1].pk, False, 4)


# --------------------------------------------------------------------------- cards


def _dashboard(team: SimpleNamespace, role: str) -> str:
    response = client_for(team, role).get(
        reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk})
    )
    assert response.status_code == 200
    return html_module.unescape(response.content.decode())


def _card(page: str, key: str) -> str:
    match = re.search(rf'<section class="block" data-block="{key}">.*?</section>', page, re.S)
    assert match is not None, key
    return match.group(0)


def _plain(fragment: str) -> str:
    return re.sub(r"[ \t\r\n]+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def test_the_file_card_lists_three_items_with_links(team: SimpleNamespace) -> None:
    from apps.admin_file.models import RequiredItem

    first = _items(team, "student")[0]
    RequiredItem.objects.filter(pk=first.pk).update(due_date=D(2027, 1, 10))
    card = _card(_dashboard(team, "student"), "admin_file")
    links = re.findall(r'<a href="/t/[^"]+/admin-file/[0-9a-f-]{36}/">', card)
    assert len(links) == 3
    assert "en retard" in _plain(card)


def test_the_conferences_card_counts_by_status(team: SimpleNamespace) -> None:
    from apps.conferences.tests.helpers import submission_of

    submission_of(team)
    submission_of(team, title="Deuxième article fictif")
    card = _card(_dashboard(team, "co_supervisor"), "conferences")
    assert re.search(r"En préparation\s*:?\s*2", _plain(card).replace("\u00a0", " "))


def _store(team: SimpleNamespace, title: str, level: L) -> Any:
    from apps.vault import services as vault

    return vault.store(
        thesis=team.thesis,
        upload=SimpleUploadedFile("piece.pdf", b"%PDF-1.7\n%fictif\n%%EOF\n"),
        title=title,
        level=level,
        user=team.student,
    )


def test_the_documents_card_lists_the_latest_three(
    team: SimpleNamespace, time_machine: Any
) -> None:
    for n, (title, level) in enumerate(
        [("Note fictive", L.PUBLIC), ("Note fictive", L.PUBLIC), ("Plan fictif", L.INTERNAL_ST),
         ("Résumé fictif", L.INTERNAL_LIRMM)]
    ):  # fmt: skip
        time_machine.move_to(NOW + dt.timedelta(minutes=n + 1), tick=False)
        _store(team, title, level)
    card = _card(_dashboard(team, "student"), "documents")
    titles = re.findall(r'<a href="/t/[^"]+/documents/[0-9a-f-]{36}/">([^<]+)</a>', card)
    assert titles == ["Résumé fictif", "Plan fictif", "Note fictive (2)"]
    assert card.count('class="badge') >= 3 and "level-badge" in card
    co_sup = _card(_dashboard(team, "co_supervisor"), "documents")  # no INTERNAL_ST
    assert "Plan fictif" not in co_sup
    assert re.findall(r'documents/[0-9a-f-]{36}/">([^<]+)</a>', co_sup)[:2] == [
        "Résumé fictif",
        "Note fictive (2)",
    ]


def test_the_team_card_by_role_and_the_end_of_the_csi(team: SimpleNamespace) -> None:
    from apps.accounts.models import Membership

    card = _plain(_card(_dashboard(team, "student"), "team"))
    assert "Directeur" in card and "Tuteur" in card
    assert "prend fin" not in card  # the CSI ends in 60 days
    Membership.objects.filter(thesis=team.thesis, role=Role.CSI_MEMBER).update(
        active_until=dt.datetime(2027, 2, 3, 12, 0, tzinfo=dt.UTC)
    )
    card = _plain(_card(_dashboard(team, "student"), "team"))
    assert "prend fin le 3\u00a0févr." in card
