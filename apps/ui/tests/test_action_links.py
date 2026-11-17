"""No action left as an underlined link (frontend step 2, after the review of the
meeting page): « Modifier », « Télécharger », « Prévoir une mission », « Afficher
les commentaires »... are discreet buttons with an icon; « Annuler la mission »
is behind the common <details> confirmation (irreversible).

The static scan names the action labels: a link whose text is one of them must
carry the btn class (ui/action.html always does).
"""

from __future__ import annotations

import html as html_module
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

BASE_DIR = Path(__file__).resolve().parents[3]
ACTIONS = (
    "Edit",
    "Duplicate",
    "Download",
    "Plan a mission",
    "Show the comments",
    "Add to my calendar (.ics)",
    "Add my missions to my calendar (.ics)",
)
# A link (no btn class) whose text, after an optional icon, is an action label.
LINK_ACTION = re.compile(
    r'<a (?![^>]*class="[^"]*\bbtn\b)[^>]*>\s*(?:\{%\s*icon[^%]*%\}\s*)?(?:<span>)?\s*'
    r'\{%\s*translate\s+"(' + "|".join(re.escape(a) for a in ACTIONS) + r')"\s*%\}'
)


def _templates() -> list[Path]:
    return sorted((BASE_DIR / "templates").rglob("*.html"))


def test_no_action_is_an_underlined_link() -> None:
    found = [
        (str(p.relative_to(BASE_DIR)), label)
        for p in _templates()
        for label in LINK_ACTION.findall(p.read_text(encoding="utf-8"))
    ]
    assert found == []


def test_the_scan_sees_what_it_must() -> None:
    assert LINK_ACTION.findall('<a href="x">{% translate "Edit" %}</a>') == ["Edit"]
    assert not LINK_ACTION.findall('<a class="btn btn-ghost" href="x">{% translate "Edit" %}</a>')
    assert not LINK_ACTION.findall('<a href="x">{% translate "Attachment" %}</a>')


# --------------------------------------------------------------------------- mission


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    from apps.travel.tests.helpers import NOW, make_team, mission_of

    time_machine.move_to(NOW, tick=False)
    team = make_team()
    team.mission = mission_of(team)
    return team


def _mission_page(team: SimpleNamespace, role: str) -> str:
    client = Client()
    client.force_login(getattr(team, role))
    kwargs = {"thesis_id": team.thesis.pk, "mission_id": team.mission.pk}
    response = client.get(reverse("travel:mission", kwargs=kwargs))
    assert response.status_code == 200
    return html_module.unescape(response.content.decode())


@pytest.mark.django_db(transaction=True)
def test_cancelling_a_mission_asks_for_a_confirmation(team: SimpleNamespace) -> None:
    page = _mission_page(team, "student")
    kwargs = {"thesis_id": team.thesis.pk, "mission_id": team.mission.pk}
    url = reverse("travel:cancel_mission", kwargs=kwargs)
    confirm = re.search(
        rf'<details class="confirm">(?:(?!</details>).)*?{url}.*?</details>', page, re.S
    )
    assert confirm is not None
    assert re.search(
        r'<summary class="btn btn-danger">.*?Annuler la mission</summary>', confirm.group(0), re.S
    )
    assert page.count(f'action="{url}"') == 1  # the only form that cancels
    edit = reverse("travel:edit_mission", kwargs=kwargs)
    header = re.search(r'<header class="page-header">.*?</header>', page, re.S)
    assert header is not None
    assert re.search(rf'<a class="btn btn-ghost" href="{edit}">', header.group(0))


@pytest.mark.django_db(transaction=True)
def test_a_reader_gets_neither_edit_nor_cancel(team: SimpleNamespace) -> None:
    page = _mission_page(team, "director")
    kwargs = {"thesis_id": team.thesis.pk, "mission_id": team.mission.pk}
    assert reverse("travel:edit_mission", kwargs=kwargs) not in page
    assert reverse("travel:cancel_mission", kwargs=kwargs) not in page


def test_download_is_a_small_discreet_button() -> None:
    source = (BASE_DIR / "templates" / "vault" / "document_detail.html").read_text("utf-8")
    assert re.search(
        r'<a class="btn btn-ghost btn-sm" href="\{% url \'vault:download\'[^>]*>'
        r'\{% icon "download" %\}',
        source,
    )


@pytest.mark.parametrize(
    ("template", "label"),
    [
        ("conferences/submission.html", "Edit"),
        ("travel/mission.html", "Edit"),
        ("objectives/detail.html", "Edit"),
    ],
)
def test_edit_is_in_the_page_header(template: str, label: str) -> None:
    source = (BASE_DIR / "templates" / template).read_text("utf-8")
    actions = re.search(r"\{% actions %\}(.*?)\{% endpage_header %\}", source, re.S)
    assert actions is not None, template
    assert f'label=_("{label}") icon="pencil"' in actions.group(1), template
