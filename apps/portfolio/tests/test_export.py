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
