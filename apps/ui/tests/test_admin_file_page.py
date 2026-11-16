"""Administrative file (frontend step 2, point 12, and docs/ui-notes.md):

- the status of an item is a badge (a word and an icon, never the colour alone);
- « en retard » is for an item still to deposit (TODO, REJECTED) past its due
  date; a deposited item reads « en attente de validation depuis X jours »
  (RequiredItem.submitted_at, Paris dates);
- an item whose validator role nobody holds in the team says so, with a link
  to invite them for whoever may invite;
- the page of one item has one title (no label twice).
"""

from __future__ import annotations

import datetime as dt
import html as html_module
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory
from apps.admin_file import services
from apps.admin_file.models import ItemStatus, RequiredItem
from apps.admin_file.tests.helpers import item_of, make_team, put

pytestmark = pytest.mark.django_db(transaction=True)

MONDAY = dt.datetime(2027, 3, 1, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team(mfa=True)


def _file(team: SimpleNamespace, role: str = "student") -> str:
    client = Client()
    client.force_login(getattr(team, role))
    response = client.get(reverse("admin_file:file", kwargs={"thesis_id": team.thesis.pk}))
    assert response.status_code == 200
    return html_module.unescape(response.content.decode())


def _article(page: str, item: RequiredItem) -> str:
    match = re.search(rf'<article [^>]*id="item-{item.pk}">.*?</article>', page, re.S)
    assert match is not None
    return match.group(0)


def _text(fragment: str) -> str:
    return re.sub(r"[ \t\r\n]+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


@pytest.mark.parametrize(
    ("status", "tone", "word"),
    [
        (ItemStatus.TODO, "neutral", "À faire"),
        (ItemStatus.SUBMITTED, "info", "Déposée"),
        (ItemStatus.VALIDATED, "success", "Validée"),
        (ItemStatus.REJECTED, "danger", "Refusée"),
    ],
)
def test_the_status_is_a_badge(team: SimpleNamespace, status: str, tone: str, word: str) -> None:
    item = put(item_of(team), status, team.student)
    article = _article(_file(team), item)
    badge = re.search(rf'<span class="badge badge-{tone}">.*?</span>', article, re.S)
    assert badge is not None and _text(badge.group(0)) == word


def test_overdue_is_for_an_item_to_deposit(team: SimpleNamespace, time_machine: Any) -> None:
    time_machine.move_to(MONDAY, tick=False)
    todo = item_of(team, due_date=dt.date(2027, 2, 20))
    rejected = put(item_of(team, due_date=dt.date(2027, 2, 20)), ItemStatus.REJECTED, team.student)
    deposited = item_of(team, due_date=dt.date(2027, 2, 20))
    services.submit(item=deposited, user=team.student)
    page = _file(team)
    assert "en retard" in _text(_article(page, todo))
    assert "en retard" in _text(_article(page, rejected))
    assert "en retard" not in _text(_article(page, deposited))


def test_a_deposited_item_waits_since_its_deposit(team: SimpleNamespace, time_machine: Any) -> None:
    time_machine.move_to(MONDAY, tick=False)
    item = item_of(team)
    services.submit(item=item, user=team.student)
    assert "en attente de validation depuis aujourd'hui" in _text(_article(_file(team), item))
    time_machine.move_to(MONDAY + dt.timedelta(days=1), tick=False)
    assert "en attente de validation depuis 1 jour" in _text(_article(_file(team), item))
    time_machine.move_to(MONDAY + dt.timedelta(days=12), tick=False)
    assert "en attente de validation depuis 12 jours" in _text(_article(_file(team), item))


def test_a_deposit_older_than_the_field_waits_without_a_number(team: SimpleNamespace) -> None:
    item = put(item_of(team), ItemStatus.SUBMITTED, team.student)  # submitted_at is NULL
    text = _text(_article(_file(team), item))
    assert "en attente de validation" in text and "depuis" not in text


def _team_without_tutor() -> SimpleNamespace:
    thesis = ThesisFactory.create()

    def member(role: Role) -> Any:
        return MembershipFactory.create(thesis=thesis, role=role).user

    return SimpleNamespace(
        thesis=thesis, student=member(Role.PHD_STUDENT), director=member(Role.DIRECTOR)
    )


def test_a_validator_nobody_holds_is_named(team: SimpleNamespace) -> None:
    lonely = _team_without_tutor()
    item = item_of(lonely, validator_roles=[Role.INDUSTRIAL_TUTOR])
    team_url = reverse("accounts:team", kwargs={"thesis_id": lonely.thesis.pk})
    student = _article(_file(lonely, "student"), item)
    hint = re.search(r'<p class="missing-validator">.*?</p>', student, re.S)
    assert hint is not None
    assert "Personne n'a le rôle « " in _text(hint.group(0))
    assert f'href="{team_url}"' in hint.group(0)  # the student invites
    director = _article(_file(lonely, "director"), item)
    hint = re.search(r'<p class="missing-validator">.*?</p>', director, re.S)
    assert hint is not None and "href=" not in hint.group(0)  # no right to invite


def test_no_hint_when_the_role_is_held_or_the_item_validated(team: SimpleNamespace) -> None:
    held = item_of(team, validator_roles=[Role.INDUSTRIAL_TUTOR])
    lonely = _team_without_tutor()
    done = put(
        item_of(lonely, validator_roles=[Role.INDUSTRIAL_TUTOR]),
        ItemStatus.VALIDATED,
        lonely.student,
    )
    assert "missing-validator" not in _article(_file(team), held)
    assert "missing-validator" not in _article(_file(lonely), done)


def test_the_item_page_has_one_title(team: SimpleNamespace) -> None:
    item = item_of(team, label="Pièce fictive unique")
    client = Client()
    client.force_login(team.student)
    url = reverse("admin_file:item", kwargs={"thesis_id": team.thesis.pk, "item_id": item.pk})
    page = html_module.unescape(client.get(url).content.decode())
    main = re.search(r"<main\b.*?</main>", page, re.S)
    assert main is not None
    assert main.group(0).count("Pièce fictive unique") == 1
    assert re.search(r"<h1>\s*Pièce du dossier administratif\s*</h1>", main.group(0))


def test_submitted_at_follows_the_cycle(team: SimpleNamespace, time_machine: Any) -> None:
    time_machine.move_to(MONDAY, tick=False)
    item = item_of(team)
    services.submit(item=item, user=team.student)
    item.refresh_from_db()
    assert item.submitted_at == timezone.now()
    services.withdraw(item=item, user=team.student)
    item.refresh_from_db()
    assert item.submitted_at is None
    services.submit(item=item, user=team.student)
    services.reject(
        item=RequiredItem.objects.get(pk=item.pk), user=team.director, reason="ILLEGIBLE"
    )
    item.refresh_from_db()
    assert item.submitted_at is None


def test_item_actions_keep_their_width() -> None:
    """« Rouvrir » no longer takes the whole width, and the due-date form no longer
    overlaps it (docs/ui-notes.md): buttons at their own width, fields too."""
    from pathlib import Path

    css = (Path(__file__).resolve().parents[3] / "static" / "css" / "components.css").read_text(
        encoding="utf-8"
    )
    rules = dict((sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css))
    assert re.search(r"justify-self\s*:\s*start", rules.get(".item form button", ""))
    assert re.search(r"width\s*:\s*auto", rules.get(".item-inline input", ""))
