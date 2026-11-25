"""Trainings (M9a), the services: validation, rights, totals, sealing, the ADUM state,
the re-registration date (docs/modules/M09-portfolio.md)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection

from apps.portfolio import services
from apps.portfolio.models import Training, TrainingCategory
from apps.portfolio.tests import helpers as h
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)
L = h.L
C = TrainingCategory


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _totals(user: Any, team: SimpleNamespace) -> dict[str, tuple[Decimal, int]]:
    listing = services.training_list(user, team.thesis)
    return {t.category.value: (t.hours, t.count) for t in listing.totals}


# --------------------------------------------------------------------------- totals


def test_the_totals_per_category_are_exact(team: SimpleNamespace) -> None:
    h.training(team, category="SCIENTIFIC", hours="12,5")
    h.training(team, category="SCIENTIFIC", hours="3.5")
    h.training(team, category="TRANSVERSAL", hours="0,5")
    h.training(team, category="INTEGRITY", hours="2")
    totals = _totals(team.student, team)
    assert totals["SCIENTIFIC"] == (Decimal("16.0"), 2)
    assert totals["TRANSVERSAL"] == (Decimal("0.5"), 1)
    assert totals["INTEGRITY"] == (Decimal("2.0"), 1)
    assert totals["ED"] == (Decimal("0"), 0) and totals["OTHER"] == (Decimal("0"), 0)
    listing = services.training_list(team.student, team.thesis)
    assert listing.total_hours == Decimal("18.5")
    assert [t.category for t in listing.totals] == list(C)  # every category, in order


def test_a_training_to_come_is_not_counted(team: SimpleNamespace) -> None:
    h.training(team, hours="4", start=h.D(2027, 1, 19), end=h.D(2027, 1, 20))  # ends today
    h.training(team, hours="7", start=h.D(2027, 1, 18), end=h.D(2027, 1, 21))  # tomorrow
    listing = services.training_list(team.student, team.thesis)
    assert listing.total_hours == Decimal("4.0")
    assert [r.training.hours for r in listing.done] == [Decimal("4.0")]
    assert [r.training.hours for r in listing.upcoming] == [Decimal("7.0")]


def test_the_totals_count_only_what_the_reader_reads(team: SimpleNamespace) -> None:
    for level, hours in ((L.PUBLIC, "1"), (L.INTERNAL_LIRMM, "2"), (L.INTERNAL_ST, "4"),
                         (L.CONFIDENTIAL, "8")):  # fmt: skip
        h.training(team, level, hours=hours)
    expected = {"student": "15", "director": "11", "co_supervisor": "3", "tutor": "13", "csi": "1"}
    for attr, hours in expected.items():
        listing = services.training_list(getattr(team, attr), team.thesis)
        assert listing.total_hours == Decimal(hours), attr
        assert listing.partial is (attr != "student"), attr


def test_the_integrity_training_is_done_only_when_finished_and_read(
    team: SimpleNamespace,
) -> None:
    assert not services.training_list(team.student, team.thesis).integrity_done
    h.training(team, L.INTERNAL_LIRMM, category="INTEGRITY", hours="3")
    h.training(team, category="INTEGRITY", hours="3", start=h.D(2027, 2, 1))  # to come
    assert services.training_list(team.student, team.thesis).integrity_done
    assert not services.training_list(team.csi, team.thesis).integrity_done  # PUBLIC only


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize("hours", ["0", "0,3", "300,5", "-1", "abc", "", "NaN", "1e3"])
def test_hours_are_half_hours_from_half_an_hour_to_300(team: SimpleNamespace, hours: str) -> None:
    with pytest.raises(ValidationError):
        h.training(team, hours=hours)
    assert not Training.objects.exists()


@pytest.mark.parametrize("hours", ["0,5", "300", "1.5", "12,5"])
def test_valid_hours(team: SimpleNamespace, hours: str) -> None:
    assert h.training(team, hours=hours).hours == Decimal(hours.replace(",", "."))


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ((2027, 2, 2), (2027, 2, 1)),
        ((2025, 11, 30), (2025, 11, 30)),
        ((2030, 12, 1), (2030, 12, 1)),
    ],
)
def test_dates_are_ordered_and_bounded(
    team: SimpleNamespace, start: tuple[int, int, int], end: tuple[int, int, int]
) -> None:
    with pytest.raises(ValidationError):
        h.training(team, start=h.D(*start), end=h.D(*end))


def test_the_bounds_are_a_year_around_the_thesis(team: SimpleNamespace) -> None:
    assert services.date_bounds(team.thesis) == (h.D(2025, 12, 1), h.D(2030, 11, 30))
    h.training(team, start=h.D(2025, 12, 1))


def test_title_required_and_texts_bounded(team: SimpleNamespace) -> None:
    cases: list[dict[str, Any]] = [
        {"title": "  "}, {"title": "x" * 201}, {"provider": "y" * 201},
        {"notes": "z" * 2001}, {"title": "Avec\x07contrôle"},
    ]  # fmt: skip
    for bad in cases:
        with pytest.raises(ValidationError):
            h.training(team, **bad)


def test_unknown_category_refused(team: SimpleNamespace) -> None:
    with pytest.raises(ValidationError):
        h.training(team, category="ADUM")


def test_a_certificate_of_another_thesis_is_refused(team: SimpleNamespace) -> None:
    from apps.activity.tests.helpers import document

    other = h.make_team()
    foreign = document(other, L.PUBLIC, title="Attestation d'une autre thèse")
    with pytest.raises(ValidationError):
        h.training(team, certificate=foreign)
    mine = document(team, L.PUBLIC, title="Attestation fictive")
    assert h.training(team, certificate=mine).certificate == mine


# --------------------------------------------------------------------------- rights


@pytest.mark.parametrize("attr", h.NOT_STUDENT)
def test_only_the_student_creates_and_the_refusal_is_audited(
    team: SimpleNamespace, attr: str
) -> None:
    from apps.portfolio import services as s

    with pytest.raises(PermissionDenied):
        s.create_training(thesis=team.thesis, user=getattr(team, attr), title="Formation",
                          category="SCIENTIFIC", hours="2", start_date=h.D(2027, 1, 11),
                          end_date=h.D(2027, 1, 11))  # fmt: skip
    assert not Training.objects.exists()
    assert AuditEvent.objects.filter(action="PORTFOLIO_ACCESS_DENIED").exists()


@pytest.mark.parametrize("attr", h.NOT_STUDENT)
def test_only_the_student_edits_marks_and_sets_the_date(team: SimpleNamespace, attr: str) -> None:
    training = h.training(team)
    user = getattr(team, attr)
    with pytest.raises(PermissionDenied):
        services.update_training(training=training, user=user, values={})
    with pytest.raises(PermissionDenied):
        services.set_adum_status(training=training, user=user, status="ENTERED")
    with pytest.raises(PermissionDenied):
        services.change_training_level(training=training, user=user, level=L.CONFIDENTIAL,
                                       recent_mfa=True)  # fmt: skip
    with pytest.raises(PermissionDenied):
        services.set_reregistration_date(thesis=team.thesis, user=user, day=h.D(2027, 9, 1))


@pytest.mark.parametrize("attr", h.NOT_READERS)
def test_who_does_not_read_the_trainings_lists_nothing(team: SimpleNamespace, attr: str) -> None:
    h.training(team)
    with pytest.raises(PermissionDenied):
        services.training_list(getattr(team, attr), team.thesis)


def test_a_hidden_training_answers_as_an_unknown_one(team: SimpleNamespace) -> None:
    hidden = h.training(team, L.INTERNAL_ST)
    for training_id in (hidden.pk, "00000000-0000-4000-8000-000000000000", "pas-un-id"):
        with pytest.raises(PermissionDenied):
            services.training_for(team.director, team.thesis, training_id, operation="detail")
    reasons = list(
        AuditEvent.objects.filter(action="PORTFOLIO_ACCESS_DENIED").values_list(
            "details_json__reason", flat=True
        )
    )
    assert reasons == ["unreadable_level", "unknown_training", "unknown_training"]


def test_a_training_of_another_thesis_is_unknown(team: SimpleNamespace) -> None:
    other = h.make_team()
    foreign = h.training(other)
    with pytest.raises(PermissionDenied):
        services.training_for(team.student, team.thesis, foreign.pk, operation="detail")


def test_the_notes_are_the_student_s_only(team: SimpleNamespace) -> None:
    h.training(team, notes="TEMOIN-FUITE-NOTE-FORMATION")
    assert services.training_list(team.student, team.thesis).done[0].notes
    for attr in ("director", "co_supervisor", "tutor", "csi"):
        row = services.training_list(getattr(team, attr), team.thesis).done[0]
        assert row.notes == "", attr


def test_a_certificate_is_shown_only_to_whoever_reads_it(team: SimpleNamespace) -> None:
    from apps.activity.tests.helpers import document

    doc = document(team, L.INTERNAL_ST, title="Attestation fictive ST")
    h.training(team, certificate=doc)
    seen = {attr: services.training_list(getattr(team, attr), team.thesis).done[0].certificate
            for attr in ("student", "director", "co_supervisor", "tutor", "csi")}  # fmt: skip
    assert seen == {"student": doc, "director": None, "co_supervisor": None, "tutor": doc,
                    "csi": None}  # fmt: skip


# --------------------------------------------------------------------------- sealing


def test_no_text_in_clear_in_the_database(team: SimpleNamespace) -> None:
    witnesses = ("TEMOIN-FUITE-INTITULE-FORMATION", "TEMOIN-FUITE-ORGANISME",
                 "TEMOIN-FUITE-NOTES-FORMATION")  # fmt: skip
    h.training(team, title=witnesses[0], provider=witnesses[1], notes=witnesses[2])
    with connection.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = [row[0] for row in cursor.fetchall()]
        for table in tables:
            query = f'SELECT CAST(t AS text) FROM "{table}" t'  # noqa: S608  # nosec B608
            cursor.execute(query)  # the names come from pg_tables
            dump = "\n".join(row[0] for row in cursor.fetchall())
            for witness in witnesses:
                assert witness not in dump, (table, witness)


def test_the_texts_are_read_back(team: SimpleNamespace) -> None:
    h.training(team, title="Atelier fictif", provider="École fictive", notes="Note fictive")
    row = services.training_list(team.student, team.thesis).done[0]
    assert (row.title, row.provider, row.notes) == ("Atelier fictif", "École fictive",
                                                    "Note fictive")  # fmt: skip


def test_an_update_writes_and_journals_only_the_changed_fields(team: SimpleNamespace) -> None:
    training = h.training(team, title="Atelier fictif", notes="Note fictive")
    values = {"title": "Atelier fictif", "provider": "Organisme fictif", "notes": "",
              "category": "TRANSVERSAL", "hours": "6", "start_date": h.D(2027, 1, 11),
              "end_date": h.D(2027, 1, 11), "certificate": None}  # fmt: skip
    updated = services.update_training(training=training, user=team.student, values=values)
    assert updated.category == "TRANSVERSAL"
    event = AuditEvent.objects.filter(action="TRAINING_UPDATED").get()
    assert event.details_json["fields"] == ["category", "notes"]
    row = services.training_list(team.student, team.thesis).done[0]
    assert row.notes == "" and row.title == "Atelier fictif"


def test_the_level_change_needs_a_second_factor_and_reseals(team: SimpleNamespace) -> None:
    from apps.vault.errors import ReauthenticationRequiredError
    from apps.vault.models import SealedText

    training = h.training(team, title="Atelier fictif")
    with pytest.raises(ReauthenticationRequiredError):
        services.change_training_level(training=training, user=team.student,
                                       level=L.INTERNAL_ST, recent_mfa=False)  # fmt: skip
    services.change_training_level(training=training, user=team.student, level=L.INTERNAL_ST,
                                   recent_mfa=True)  # fmt: skip
    assert set(SealedText.objects.filter(object_id=training.pk).values_list("level", flat=True)) \
        == {"INTERNAL_ST"}  # fmt: skip
    event = AuditEvent.objects.filter(action="TRAINING_LEVEL_CHANGED").get()
    assert event.details_json["from"] == "PUBLIC" and event.details_json["to"] == "INTERNAL_ST"
    assert services.training_list(team.director, team.thesis).done == []
    assert services.training_list(team.tutor, team.thesis).done[0].title == "Atelier fictif"


def test_the_adum_state(team: SimpleNamespace) -> None:
    training = h.training(team)
    assert training.adum_status == "TO_ENTER"
    assert services.set_adum_status(training=training, user=team.student,
                                    status="ENTERED").adum_status == "ENTERED"  # fmt: skip
    with pytest.raises(ValidationError):
        services.set_adum_status(training=training, user=team.student, status="VALIDATED")
    assert AuditEvent.objects.filter(action="TRAINING_ADUM_STATUS_CHANGED").count() == 1


def test_the_reregistration_date(team: SimpleNamespace) -> None:
    assert services.policy_of(team.thesis).reregistration_date is None
    services.set_reregistration_date(thesis=team.thesis, user=team.student, day=h.D(2027, 9, 15))
    assert services.policy_of(team.thesis).reregistration_date == h.D(2027, 9, 15)
    services.set_reregistration_date(thesis=team.thesis, user=team.student, day=None)
    assert services.policy_of(team.thesis).reregistration_date is None
    with pytest.raises(ValidationError):
        services.set_reregistration_date(thesis=team.thesis, user=team.student,
                                         day=h.D(2035, 1, 1))  # fmt: skip
    details = [e.details_json for e in AuditEvent.objects.filter(action="PORTFOLIO_POLICY_CHANGED")
               .order_by("seq")]  # fmt: skip
    assert [(d["from"], d["to"]) for d in details] == [(None, "2027-09-15"), ("2027-09-15", None)]


@pytest.mark.parametrize("status", ["DEFENDED", "CLOSED"])
def test_a_finished_thesis_freezes_the_trainings_but_keeps_them_readable(
    team: SimpleNamespace, status: str
) -> None:
    from apps.accounts.models import Thesis

    training = h.training(team)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=status, defended_on=h.D(2027, 1, 15))
    team.thesis.refresh_from_db()
    with pytest.raises(PermissionDenied):
        h.training(team)
    with pytest.raises(PermissionDenied):
        services.set_adum_status(training=training, user=team.student, status="ENTERED")
    assert services.training_list(team.director, team.thesis).done
    assert services.adum_summary(team.student, team.thesis, year=1).rows
