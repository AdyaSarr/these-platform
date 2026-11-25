"""The ADUM summary (M9a): the year, PUBLIC only by default, the CSV (second factor,
journal with numbers only, formulas neutralized, separator and encoding)."""

from __future__ import annotations

import csv
import io
from types import SimpleNamespace
from typing import Any

import pytest

from apps.portfolio import services
from apps.portfolio.tests import helpers as h
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)
L = h.L


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _rows(content: bytes) -> list[list[str]]:
    assert content.startswith("﻿".encode())
    text = content.decode("utf-8").removeprefix("﻿")
    assert "\r\n" in text
    return list(csv.reader(io.StringIO(text), delimiter=";"))


def test_the_summary_takes_the_finished_trainings_of_the_academic_year(
    team: SimpleNamespace,
) -> None:
    h.training(team, title="Dans l'année", start=h.D(2026, 12, 10))
    h.training(team, title="Année précédente", start=h.D(2026, 8, 31))
    h.training(team, title="À venir", start=h.D(2027, 2, 1))
    summary = services.adum_summary(team.student, team.thesis, year=1)
    assert [r.title for r in summary.rows] == ["Dans l'année"]
    assert summary.year.number == 1
    default = services.adum_summary(team.student, team.thesis)  # today's academic year
    assert default.year.number == 1


def test_the_internal_trainings_are_left_out_by_default(team: SimpleNamespace) -> None:
    h.training(team, title="Publique")
    h.training(team, L.INTERNAL_ST, title="TEMOIN-FUITE-FORMATION-ST")
    summary = services.adum_summary(team.student, team.thesis, year=1)
    assert [r.title for r in summary.rows] == ["Publique"] and summary.left_out == 1
    full = services.adum_summary(team.student, team.thesis, year=1, include_internal=True)
    assert {r.title for r in full.rows} == {"Publique", "TEMOIN-FUITE-FORMATION-ST"}
    assert full.left_out == 0
    _name, content = services.adum_csv(thesis=team.thesis, user=team.student, year=1,
                                       include_internal=False, recent_mfa=True)  # fmt: skip
    assert b"TEMOIN-FUITE-FORMATION-ST" not in content


def test_the_csv(team: SimpleNamespace) -> None:
    h.training(team, title="Atelier fictif", provider="École fictive", hours="12,5",
               category="TRANSVERSAL", start=h.D(2027, 1, 11), end=h.D(2027, 1, 12))  # fmt: skip
    name, content = services.adum_csv(thesis=team.thesis, user=team.student, year=1,
                                      include_internal=False, recent_mfa=True)  # fmt: skip
    assert name == "formations-adum-2026-2027.csv"
    rows = _rows(content)
    assert rows[0] == ["Intitulé", "Organisme", "Catégorie", "Heures", "Début", "Fin", "Niveau"]
    assert rows[1][0:2] == ["Atelier fictif", "École fictive"]
    assert rows[1][3:6] == ["12,5", "11/01/2027", "12/01/2027"]


@pytest.mark.parametrize("start", ["=", "+", "-", "@", "\t", "\r"])
def test_a_formula_is_neutralized(team: SimpleNamespace, start: str) -> None:
    h.training(team, title=f"{start}HYPERLINK(1)" if start not in "\t\r" else "Formation",
               provider=f"{start}SUM(A1)" if start not in "\t\r" else "Organisme")  # fmt: skip
    assert services.neutralized(f"{start}cmd") == f"'{start}cmd"
    _name, content = services.adum_csv(thesis=team.thesis, user=team.student, year=1,
                                       include_internal=False, recent_mfa=True)  # fmt: skip
    for cell in _rows(content)[1][:2]:
        assert not cell.startswith(services.FORMULA_STARTS), cell


def test_the_csv_needs_a_second_factor_and_is_journaled_without_text(
    team: SimpleNamespace,
) -> None:
    from apps.vault.errors import ReauthenticationRequiredError

    h.training(team, title="TEMOIN-FUITE-JOURNAL-CSV")
    with pytest.raises(ReauthenticationRequiredError):
        services.adum_csv(thesis=team.thesis, user=team.student, year=1, include_internal=False,
                          recent_mfa=False)  # fmt: skip
    assert not AuditEvent.objects.filter(action="PORTFOLIO_EXPORTED").exists()
    services.adum_csv(thesis=team.thesis, user=team.student, year=1, include_internal=True,
                      recent_mfa=True)  # fmt: skip
    event = AuditEvent.objects.filter(action="PORTFOLIO_EXPORTED").get()
    assert event.details_json == {"year": 1, "rows": 1, "internal": True}


def test_the_csv_view_asks_for_a_recent_second_factor(team: SimpleNamespace) -> None:
    h.training(team, title="Atelier fictif")
    page, path = h.url(team, "adum"), h.url(team, "adum_csv")
    stale = h.browser_post(h.browser_for(team.student), page, path, {"year": "1"})
    assert stale.status_code == 302 and "reauthenticate" in stale["Location"]
    response = h.browser_post(h.mfa_browser(team.student), page, path, {"year": "1"})
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"] == 'attachment; filename="formations-adum-2026-2027.csv"'
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "no-store" in response["Cache-Control"]
    assert _rows(response.content)[1][0] == "Atelier fictif"


@pytest.mark.parametrize("attr", h.NOT_STUDENT)
def test_the_summary_is_the_student_s_only(team: SimpleNamespace, attr: str) -> None:
    client = h.client_for(getattr(team, attr))
    assert client.get(h.url(team, "adum")).status_code == 403
    assert client.post(h.url(team, "adum_csv"), {"year": "1"}).status_code == 403


def test_the_summary_page(team: SimpleNamespace) -> None:
    h.training(team, title="Atelier fictif")
    h.training(team, L.CONFIDENTIAL, title="Atelier confidentiel")
    client = h.client_for(team.student)
    body = client.get(h.url(team, "adum")).content.decode()
    assert "Atelier fictif" in body and "Atelier confidentiel" not in body
    assert "1 formation interne de cette année est laissée de côté" in body
    full = client.get(h.url(team, "adum") + "?year=1&internal=1").content.decode()
    assert "Atelier confidentiel" in full and "à vérifier avant saisie" in full
    assert client.get(h.url(team, "adum") + "?year=9").status_code == 400
