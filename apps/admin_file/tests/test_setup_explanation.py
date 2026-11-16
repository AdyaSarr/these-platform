"""The thesis dashboard explains a SETUP status: it becomes ACTIVE when every
mandatory item of the administrative file is validated (M3 rule, unchanged).

The count comes from admin_file.services.progress, the function of the
administrative-file card: one calculation, so the line and the card always agree,
and the hidden part of the file is never counted (M3)."""

from __future__ import annotations

import datetime as dt
import html as html_module
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Thesis
from apps.admin_file import services
from apps.admin_file.models import ItemStatus, RequiredItem
from apps.admin_file.tests.helpers import make_team, put

pytestmark = pytest.mark.django_db(transaction=True)

LINE = re.compile(r'<p class="status-help"[^>]*>(.*?)</p>', re.S)


@pytest.fixture
def team() -> SimpleNamespace:
    team = make_team(mfa=True)  # the MFA middleware sends users without a factor away
    Thesis.objects.filter(pk=team.thesis.pk).update(
        cifre_start_date=dt.date(2026, 3, 2), first_enrollment_date=dt.date(2026, 3, 15)
    )
    services.seed_items(team.thesis)
    return team


def _validate(team: SimpleNamespace, how_many: int) -> None:
    items = RequiredItem.objects.filter(thesis=team.thesis, mandatory=True).order_by("code")
    for item in items[:how_many]:
        put(item, ItemStatus.VALIDATED, team.student)


def _dashboard(team: SimpleNamespace, role: str = "student") -> str:
    client = Client()
    client.force_login(getattr(team, role))
    response = client.get(reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk}))
    assert response.status_code == 200
    return html_module.unescape(response.content.decode())  # apostrophes are escaped


def _line(html: str) -> str | None:
    match = LINE.search(html)
    if not match:
        return None
    text = re.sub(r"<[^>]+>", "", match.group(1))  # the sentence is wrapped in the link
    return re.sub(r"\s+", " ", text).strip()


def _card_counts(html: str) -> tuple[int, int]:
    # The progress moved from the card to the « Administrative file » tile (frontend
    # step 8a): the same admin_file.services.progress().
    card = re.search(r'data-tile="file">(.*?)</li>', html, re.S)
    assert card is not None
    validated, total = re.search(r'<progress value="(\d+)" max="(\d+)"', card.group(1)).groups()  # type: ignore[union-attr]
    return int(validated), int(total)


def test_the_catalogue_has_ten_mandatory_items(team: SimpleNamespace) -> None:
    assert RequiredItem.objects.filter(thesis=team.thesis, mandatory=True).count() == 10


@pytest.mark.parametrize("validated", [0, 3, 10])
def test_a_setup_thesis_explains_its_status(team: SimpleNamespace, validated: int) -> None:
    _validate(team, validated)
    html = _dashboard(team)
    line = _line(html)
    assert line is not None
    assert line.startswith(
        "La thèse passera « En cours » quand les pièces obligatoires du dossier "
        "administratif seront validées"
    ), line
    assert f"({validated} / 10)" in line
    url = reverse("admin_file:file", kwargs={"thesis_id": team.thesis.pk})
    assert f'href="{url}"' in LINE.search(html).group(0)  # type: ignore[union-attr]


def test_an_active_thesis_has_no_explanation(team: SimpleNamespace) -> None:
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.ACTIVE)
    html = _dashboard(team)
    assert _line(html) is None
    assert "En cours" in html  # the status itself is shown


@pytest.mark.parametrize("role", ["student", "director", "tutor"])
def test_the_count_of_the_line_is_the_count_of_the_card(team: SimpleNamespace, role: str) -> None:
    _validate(team, 3)
    html = _dashboard(team, role)
    validated, total = _card_counts(html)
    assert f"({validated} / {total}" in (_line(html) or ""), _line(html)


def test_the_tutor_sees_the_count_of_the_part_they_see(team: SimpleNamespace) -> None:
    """The tutor reads the CIFRE part only: the hidden part is never counted (M3)."""
    html = _dashboard(team, "tutor")
    line = _line(html) or ""
    validated, total = _card_counts(html)
    assert total < 10
    assert f"({validated} / {total} que vous voyez)" in line


def test_without_access_to_the_file_no_count_and_no_link(team: SimpleNamespace) -> None:
    """The co-supervisor has no right on the administrative file: the sentence only."""
    html = _dashboard(team, "co_supervisor")
    line = _line(html)
    assert line is not None
    assert "/" not in line and "href" not in LINE.search(html).group(0)  # type: ignore[union-attr]
    assert line.endswith("seront validées.")


def test_dates_in_french(team: SimpleNamespace) -> None:
    html = _dashboard(team)
    assert "Date d'effet CIFRE\u00a0: 2 mars 2026" in html
    assert "Première inscription\u00a0: 15 mars 2026" in html


def test_dates_are_shown_for_an_active_thesis_too(team: SimpleNamespace) -> None:
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.ACTIVE)
    html = _dashboard(team, "director")
    assert "Date d'effet CIFRE\u00a0: 2 mars 2026" in html


def test_one_calculation_for_the_line_and_the_card(monkeypatch: Any, team: SimpleNamespace) -> None:
    """Both go through services.progress: patching it changes both."""
    real = services.progress
    calls: list[Any] = []

    def counting(user: Any, thesis: Any) -> Any:
        calls.append(user)
        return real(user, thesis)

    monkeypatch.setattr(services, "progress", counting)
    _dashboard(team)
    assert len(calls) == 2  # the card, then the line: the same function
