"""The pages of the trainings (M9a): for each view, allowed and refused per role;
POSTs like a browser (CSRF token, Origin); the CSI's view; the second factor."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.portfolio import services
from apps.portfolio.models import Training
from apps.portfolio.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)
L = h.L


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _get(team: SimpleNamespace, attr: str, path: str) -> Any:
    return h.client_for(getattr(team, attr)).get(path)


# --------------------------------------------------------------------------- reading


@pytest.mark.parametrize("attr", h.READERS)
def test_the_readers_open_the_list_and_a_public_training(team: SimpleNamespace, attr: str) -> None:
    training = h.training(team, title="TEMOIN-FUITE-TITRE-PUBLIC")
    body = _get(team, attr, h.url(team, "trainings")).content.decode()
    assert "TEMOIN-FUITE-TITRE-PUBLIC" in body
    response = _get(team, attr, h.url(team, "detail", training_id=training.pk))
    assert response.status_code == 200
    assert "TEMOIN-FUITE-TITRE-PUBLIC" in response.content.decode()


@pytest.mark.parametrize("attr", h.NOT_READERS)
def test_the_others_are_refused(team: SimpleNamespace, attr: str) -> None:
    training = h.training(team)
    for path in (h.url(team, "trainings"), h.url(team, "detail", training_id=training.pk)):
        assert _get(team, attr, path).status_code == 403, path


def test_the_mention_on_the_totals_follows_the_roles(team: SimpleNamespace) -> None:
    mention = "Calculé sur les formations que vous pouvez lire"
    assert mention not in _get(team, "student", h.url(team, "trainings")).content.decode()
    for attr in ("director", "co_supervisor", "tutor", "csi"):
        assert mention in _get(team, attr, h.url(team, "trainings")).content.decode(), attr


def test_the_csi_member_sees_the_public_trainings_totals_and_integrity_only(
    team: SimpleNamespace,
) -> None:
    from apps.activity.tests.helpers import document

    doc = document(team, L.PUBLIC, title="TEMOIN-FUITE-ATTESTATION")
    shown = h.training(team, title="TEMOIN-FUITE-PUBLIQUE", category="INTEGRITY", hours="3",
                       notes="TEMOIN-FUITE-NOTE", certificate=doc)  # fmt: skip
    h.training(team, L.INTERNAL_LIRMM, title="TEMOIN-FUITE-INTERNE", hours="40")
    page = _get(team, "csi", h.url(team, "trainings")).content.decode()
    detail = _get(team, "csi", h.url(team, "detail", training_id=shown.pk)).content.decode()
    for body in (page, detail):
        assert "TEMOIN-FUITE-PUBLIQUE" in body
        for absent in ("TEMOIN-FUITE-INTERNE", "TEMOIN-FUITE-NOTE", "TEMOIN-FUITE-ATTESTATION",
                       str(doc.pk), "ADUM"):  # fmt: skip
            assert absent not in body, absent
    assert "intégrité scientifique&nbsp;: faite" in page or "intégrité scientifique : faite" in page
    assert "3&nbsp;h" in page and "40" not in page.split("Heures suivies")[1].split("</table>")[0]


def test_the_student_sees_notes_certificate_and_adum_state(team: SimpleNamespace) -> None:
    from apps.activity.tests.helpers import document

    doc = document(team, L.PUBLIC, title="Attestation fictive")
    training = h.training(team, notes="Note fictive du doctorant", certificate=doc)
    body = _get(team, "student", h.url(team, "detail", training_id=training.pk)).content.decode()
    assert "Note fictive du doctorant" in body and str(doc.pk) in body and "ADUM" in body


def test_an_internal_training_answers_as_an_unknown_one(team: SimpleNamespace) -> None:
    hidden = h.training(team, L.INTERNAL_ST)
    a = _get(team, "director", h.url(team, "detail", training_id=hidden.pk))
    unknown = "00000000-0000-4000-8000-000000000000"
    b = _get(team, "director", h.url(team, "detail", training_id=unknown))
    assert a.status_code == b.status_code == 403


# --------------------------------------------------------------------------- writing


def test_the_student_adds_a_training_like_a_browser(team: SimpleNamespace) -> None:
    client = h.browser_for(team.student)
    new = h.url(team, "new")
    data = {"title": "Atelier fictif", "provider": "École fictive", "category": "SCIENTIFIC",
            "hours": "7,5", "start_date": "2027-01-12", "end_date": "2027-01-13",
            "certificate": "", "notes": "", "level": "PUBLIC"}  # fmt: skip
    response = h.browser_post(client, new, new, data)
    training = Training.objects.get()
    assert response.status_code == 302
    assert response["Location"] == h.url(team, "detail", training_id=training.pk)
    assert str(training.hours) == "7.5"


def test_an_invalid_training_is_shown_again(team: SimpleNamespace) -> None:
    client = h.browser_for(team.student)
    new = h.url(team, "new")
    data = {"title": "Atelier fictif", "category": "SCIENTIFIC", "hours": "0,2",
            "start_date": "2027-01-12", "end_date": "2027-01-12", "level": "PUBLIC"}  # fmt: skip
    assert h.browser_post(client, new, new, data).status_code == 400
    assert not Training.objects.exists()


@pytest.mark.parametrize("attr", h.NOT_STUDENT)
def test_the_others_cannot_add_edit_mark_or_set(team: SimpleNamespace, attr: str) -> None:
    training = h.training(team)
    client = h.client_for(getattr(team, attr))
    for name, kw in (("new", {}), ("edit", {"training_id": training.pk}), ("settings", {})):
        assert client.get(h.url(team, name, **kw)).status_code == 403, name
    posts = [
        (h.url(team, "new"), {"title": "x"}),
        (h.url(team, "edit", training_id=training.pk), {"title": "x"}),
        (h.url(team, "adum_status", training_id=training.pk), {"status": "ENTERED"}),
        (h.url(team, "level", training_id=training.pk), {"level": "CONFIDENTIAL"}),
        (h.url(team, "settings"), {"reregistration_date": "2027-09-01"}),
    ]
    for path, data in posts:
        assert client.post(path, data).status_code == 403, path
    training.refresh_from_db()
    assert (training.adum_status, training.level) == ("TO_ENTER", "PUBLIC")


def test_the_student_edits_a_training(team: SimpleNamespace) -> None:
    training = h.training(team, title="Atelier fictif")
    client = h.browser_for(team.student)
    edit = h.url(team, "edit", training_id=training.pk)
    assert "Atelier fictif" in client.get(edit).content.decode()
    data = {"title": "Atelier fictif renommé", "provider": "", "category": "OTHER",
            "hours": "2", "start_date": "2027-01-11", "end_date": "2027-01-11",
            "certificate": "", "notes": ""}  # fmt: skip
    assert h.browser_post(client, edit, edit, data).status_code == 302
    row = services.training_list(team.student, team.thesis).done[0]
    assert (row.title, row.training.category) == ("Atelier fictif renommé", "OTHER")


def test_the_student_marks_a_training_entered_in_adum(team: SimpleNamespace) -> None:
    training = h.training(team)
    client = h.browser_for(team.student)
    detail = h.url(team, "detail", training_id=training.pk)
    response = h.browser_post(client, detail, h.url(team, "adum_status", training_id=training.pk),
                              {"status": "ENTERED"})  # fmt: skip
    assert response.status_code == 302
    training.refresh_from_db()
    assert training.adum_status == "ENTERED"


def test_the_level_change_asks_for_a_recent_second_factor(team: SimpleNamespace) -> None:
    training = h.training(team)
    detail = h.url(team, "detail", training_id=training.pk)
    level = h.url(team, "level", training_id=training.pk)
    stale = h.browser_post(h.browser_for(team.student), detail, level, {"level": "INTERNAL_ST"})
    assert stale.status_code == 302 and "reauthenticate" in stale["Location"]
    training.refresh_from_db()
    assert training.level == "PUBLIC"
    fresh = h.browser_post(h.mfa_browser(team.student), detail, level, {"level": "INTERNAL_ST"})
    assert fresh.status_code == 302 and fresh["Location"] == detail
    training.refresh_from_db()
    assert training.level == "INTERNAL_ST"


def test_the_student_sets_the_reregistration_date(team: SimpleNamespace) -> None:
    client = h.browser_for(team.student)
    page = h.url(team, "settings")
    response = h.browser_post(client, page, page, {"reregistration_date": "2027-09-15"})
    assert response.status_code == 302
    assert services.policy_of(team.thesis).reregistration_date == h.D(2027, 9, 15)


def test_the_tabs_follow_the_rights(team: SimpleNamespace) -> None:
    student = _get(team, "student", h.url(team, "trainings")).content.decode()
    director = _get(team, "director", h.url(team, "trainings")).content.decode()
    for name in ("adum", "settings"):
        assert h.url(team, name) in student
        assert h.url(team, name) not in director
