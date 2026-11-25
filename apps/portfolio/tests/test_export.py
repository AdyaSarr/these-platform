"""The trainings in the student's export (M13b): portfolio.json, with the texts
decrypted, at the exported levels only (INTERNAL_ST with an agreement of ST)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.export.tests import helpers as x
from apps.portfolio import services
from apps.portfolio.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)
L = h.L


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def test_the_trainings_are_exported_at_the_exported_levels(team: SimpleNamespace) -> None:
    from apps.activity.tests.helpers import document

    doc = document(team, L.PUBLIC, title="Attestation fictive")
    h.training(team, title="Atelier public fictif", notes="Note fictive", hours="12,5",
               certificate=doc)  # fmt: skip
    h.training(team, L.CONFIDENTIAL, title="Atelier confidentiel fictif")
    h.training(team, L.INTERNAL_ST, title="TEMOIN-FUITE-FORMATION-ST")
    services.set_reregistration_date(thesis=team.thesis, user=team.student, day=h.D(2027, 9, 15))
    archive = x.run(team)
    data = x.read(archive, "portfolio.json")
    titles = {t["intitule"] for t in data["formations"]}
    assert titles == {"Atelier public fictif", "Atelier confidentiel fictif"}
    public = next(t for t in data["formations"] if t["intitule"] == "Atelier public fictif")
    assert public["heures"] == "12.5" and public["notes"] == "Note fictive"
    assert public["attestation"] == str(doc.pk) and public["niveau"] == "PUBLIC"
    assert data["reinscription"] == "2027-09-15"
    assert "TEMOIN-FUITE-FORMATION-ST" not in x.everything(archive)


def test_with_the_st_agreement_internal_st_is_exported(team: SimpleNamespace) -> None:
    from apps.export import services as export

    h.training(team, L.INTERNAL_ST, title="Atelier ST fictif")
    export.grant_st_consent(thesis=team.thesis, user=team.tutor, recent_mfa=True)
    data = x.read(x.run(team), "portfolio.json")
    assert [t["intitule"] for t in data["formations"]] == ["Atelier ST fictif"]


# --------------------------------------------------------------------------- M9b


def test_m9b_in_the_export_at_the_exported_levels(team: SimpleNamespace) -> None:
    article = h.publication(team, title="Article public fictif")
    h.details(team, article, doi="10.12345/public", presented_on=h.D(2027, 1, 14))
    hidden = h.publication(team, L.INTERNAL_ST, title="Article ST fictif")
    h.details(team, hidden, volume="TEMOIN-FUITE-VOLUME-ST")
    h.teaching(team, course="Cours public fictif", hours="12,5", notes="Note fictive")
    h.teaching(team, L.INTERNAL_ST, course="TEMOIN-FUITE-COURS-ST")
    h.outreach(team, L.CONFIDENTIAL, title="Action confidentielle fictive")
    h.outreach(team, L.INTERNAL_ST, title="TEMOIN-FUITE-ACTION-ST")
    h.skill(team, title="Compétence fictive", evidence="Preuve fictive")
    services.set_career_note(thesis=team.thesis, user=team.student, text="Projet fictif")
    archive = x.run(team)
    data = x.read(archive, "portfolio.json")
    (details,) = data["publications"]
    assert details["soumission"] == str(article.pk) and details["doi"] == "10.12345/public"
    assert details["presentee_le"] == "2027-01-14" and details["categorie_adum"]
    (course,) = data["enseignements"]
    assert course["intitule"] == "Cours public fictif" and course["heures"] == "12.5"
    assert course["notes"] == "Note fictive" and course["type"] == "TD"
    assert [o["intitule"] for o in data["diffusion"]] == ["Action confidentielle fictive"]
    assert data["competences"] == [
        {
            "id": data["competences"][0]["id"],
            "intitule": "Compétence fictive",
            "preuves": "Preuve fictive",
            "categorie": "TECHNICAL",
        }
    ]
    assert data["projet_professionnel"] == "Projet fictif"
    assert "TEMOIN-FUITE" not in x.everything(archive)


def test_m9b_internal_st_with_the_st_agreement(team: SimpleNamespace) -> None:
    from apps.export import services as export

    h.teaching(team, L.INTERNAL_ST, course="Cours ST fictif")
    hidden = h.publication(team, L.INTERNAL_ST, title="Article ST fictif")
    h.details(team, hidden, volume="7")
    export.grant_st_consent(thesis=team.thesis, user=team.tutor, recent_mfa=True)
    data = x.read(x.run(team), "portfolio.json")
    assert [t["intitule"] for t in data["enseignements"]] == ["Cours ST fictif"]
    assert [p["volume"] for p in data["publications"]] == ["7"]
