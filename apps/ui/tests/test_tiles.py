"""Dashboard tiles: computed from the data of the blocks the view already filtered
(accounts.views.DASHBOARD_BLOCKS, one action each), never from anything else. A
tile without its block is absent; the alert count adds up only counts the blocks
already show."""

from __future__ import annotations

import html as html_module
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.urls import reverse

from apps.accounts.models import Thesis
from apps.accounts.roles import Role
from apps.admin_file import services as admin_file

from .helpers import signed_in


def _tiles(blocks: list[tuple[str, dict[str, Any]]]) -> Any:
    from apps.ui.tiles import dashboard_tiles

    return dashboard_tiles([(SimpleNamespace(key=key), data) for key, data in blocks])


PROGRESS = admin_file.Progress(validated=3, total=10)


def test_no_block_no_tile() -> None:
    tiles = _tiles([("team", {}), ("documents", {"count": 4})])
    assert (tiles.file, tiles.objectives, tiles.meeting, tiles.alerts) == (None, None, None, None)
    assert not tiles


def test_file_tile_is_the_progress_of_the_block() -> None:
    assert _tiles([("admin_file", {"progress": PROGRESS})]).file is PROGRESS


def test_objectives_tile() -> None:
    data = {"year": SimpleNamespace(number=1), "attainment": 42, "drafts": 1, "overdue": 2}
    assert _tiles([("objectives", data)]).objectives == data
    assert _tiles([("objectives", {})]).objectives == {}  # block shown, no year yet


def test_meeting_tile() -> None:
    meeting = SimpleNamespace(pk=1)
    data = {"next_meeting": meeting, "next_is_mine": True, "overdue_count": 0, "my_overdue": []}
    assert _tiles([("meetings", data)]).meeting == data


def test_alerts_add_up_only_what_falls_to_the_viewer() -> None:
    """My overdue actions (not every overdue action of the thesis), the approvals I
    decide, my missing receipts; the overdue objectives are in their own tile."""
    mine = [(object(), {"title": "x"})] * 2
    tiles = _tiles(
        [
            ("objectives", {"year": object(), "attainment": None, "drafts": 5, "overdue": 2}),
            ("meetings", {"next_meeting": None, "overdue_count": 3, "my_overdue": mine}),
            ("conferences", {"deadlines": [], "request_dues": [], "pending": 1}),
            ("travel", {"pending": 0, "missing_receipts": 4}),
        ]
    )
    assert tiles.alerts.total == 2 + 1 + 4
    assert tiles.alerts.parts == [
        ("actions_overdue", 2),
        ("conference_decisions", 1),
        ("missing_receipts", 4),
    ]


def test_alerts_without_any_count_block() -> None:
    assert _tiles([("admin_file", {"progress": PROGRESS})]).alerts is None


def test_alerts_with_nothing_to_report() -> None:
    alerts = _tiles([("travel", {})]).alerts
    assert alerts is not None and alerts.total == 0 and alerts.parts == []


# --------------------------------------------------------------------------- rendered

EXPECTED = {
    Role.PHD_STUDENT: ["file", "objectives", "meeting", "alerts"],
    Role.DIRECTOR: ["file", "objectives", "meeting", "alerts"],
    Role.INDUSTRIAL_TUTOR: ["file", "objectives", "meeting", "alerts"],
    Role.CO_SUPERVISOR: ["objectives", "meeting", "alerts"],  # no right on the file
}


def _dashboard(role: Role, thesis: Thesis | None = None) -> str:
    client, thesis = signed_in(role, thesis)
    response = client.get(reverse("accounts:dashboard", kwargs={"thesis_id": thesis.pk}))
    assert response.status_code == 200
    return html_module.unescape(response.content.decode())


@pytest.mark.django_db
@pytest.mark.parametrize("role", list(EXPECTED))
def test_tiles_per_role(role: Role) -> None:
    html = _dashboard(role)
    assert re.findall(r'data-tile="(\w+)"', html) == EXPECTED[role]
    # Tiles first, then the module cards (unchanged data-block markers).
    assert html.index('data-tile="') < html.index('data-block="')


@pytest.mark.django_db
def test_greeting() -> None:
    client, thesis = signed_in(Role.DIRECTOR)
    html = html_module.unescape(
        client.get(reverse("accounts:dashboard", kwargs={"thesis_id": thesis.pk})).content.decode()
    )
    user = client.get(
        reverse("accounts:dashboard", kwargs={"thesis_id": thesis.pk})
    ).wsgi_request.user
    assert re.search(r'<p class="greeting">Bonjour ' + re.escape(user.first_name), html)  # type: ignore[union-attr]


@pytest.mark.django_db(transaction=True)
def test_only_open_the_file_is_a_link_in_the_setup_sentence() -> None:
    """docs/ui-notes.md: the sentence is text; only « Ouvrir le dossier » is a link."""
    from apps.accounts.tests.factories import ThesisFactory

    thesis = ThesisFactory.create(status=Thesis.Status.SETUP)
    admin_file.seed_items(thesis)
    html = _dashboard(Role.PHD_STUDENT, thesis)
    line = re.search(r'<p class="status-help"[^>]*>(.*?)</p>', html, re.S)
    assert line is not None
    links = re.findall(r"<a [^>]*>(.*?)</a>", line.group(1), re.S)
    assert [re.sub(r"<[^>]+>", "", x).strip() for x in links] == ["Ouvrir le dossier"]


@pytest.mark.django_db
def test_percent_has_a_no_break_space() -> None:
    html = _dashboard(Role.PHD_STUDENT)
    tile = re.search(r'data-tile="file".*?</a>', html, re.S)
    assert tile is not None
    assert re.search("\\d[\u00a0\u202f]%", tile.group(0))
    assert not re.search(r"\d %", tile.group(0))
