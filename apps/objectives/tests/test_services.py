"""objectives.services: creation, approval, sealed revisions, acknowledgement, drop,
progress, comments. Rights, audit and the vault are exercised for real."""

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
import time_machine
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection

from apps.accounts.models import Thesis
from apps.accounts.permissions import Action
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.objectives import services
from apps.objectives.models import (
    Category,
    Objective,
    ObjectiveComment,
    ObjectiveRevision,
    ProgressUpdate,
    RevisedField,
    Status,
    ThesisYear,
)
from apps.objectives.tests.helpers import (
    D,
    approved,
    make_team,
    marker,
    objective_of,
    texts,
    year_of,
)
from apps.vault import formats
from apps.vault import services as vault
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import AuditEvent, SealedText

# transaction=True: every service commits, as in production (CLAUDE.md: a vault
# service after an audited one in one test transaction would hit LockOrderError).
pytestmark = pytest.mark.django_db(transaction=True)

NOW = dt.datetime(2027, 3, 15, 10, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _frozen_now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=False)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _events(action: str) -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=action).order_by("seq"))


def _revisions(objective: Objective) -> list[ObjectiveRevision]:
    return list(ObjectiveRevision.objects.filter(objective=objective).order_by("created_at", "pk"))


def _revise(team: SimpleNamespace, objective: Objective, reason: str = "", **changes: Any) -> Any:
    return services.update_objective(
        objective=objective, user=team.student, changes=changes, reason=reason
    )


# =========================================================================== creation


def test_the_student_creates_an_objective_with_sealed_texts(team: SimpleNamespace) -> None:
    objective = objective_of(team, title="Titre fictif à protéger")
    row = Objective.objects.get(pk=objective.pk)
    assert (row.thesis_id, row.year.number, row.weight, row.level) == (
        team.thesis.pk,
        1,
        2,
        L.PUBLIC,
    )
    assert (row.status, row.progress, row.approved_at, row.changed_since_approval) == (
        Status.PLANNED,
        0,
        None,
        False,
    )
    assert texts(team.director, objective) == {
        "title": "Titre fictif à protéger",
        "description": "Description fictive.",
        "success_criterion": "Un critère fictif mesurable.",
    }
    slots = set(SealedText.objects.filter(object_id=objective.pk).values_list("slot", flat=True))
    assert slots == {0x0101, 0x0102, 0x0103}
    (event,) = _events("OBJECTIVE_CREATED")
    assert event.details_json == {"year": 1, "level": "PUBLIC", "category": "RESEARCH"}


def test_public_is_the_default_level(team: SimpleNamespace) -> None:
    objective = services.create_objective(
        thesis=team.thesis,
        user=team.student,
        year=year_of(team),
        title="Titre fictif",
        category=Category.TRAINING,
        due_date=D(2027, 5, 1),
    )
    assert objective.level == L.PUBLIC
    assert objective.weight == 1
    assert not SealedText.objects.filter(object_id=objective.pk, slot=0x0102).exists()


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor", "csi", "guest", "outsider"])
def test_only_the_student_creates(team: SimpleNamespace, role: str) -> None:
    with pytest.raises(PermissionDenied):
        objective_of(team, user=getattr(team, role))
    assert not Objective.objects.exists()
    assert not SealedText.objects.exists()
    (event,) = _events("OBJECTIVE_ACCESS_DENIED")
    assert event.details_json == {"operation": "create", "reason": "missing_right"}


def test_internal_st_is_not_an_objective_level(team: SimpleNamespace) -> None:
    """The director, who approves, cannot read INTERNAL_ST."""
    with pytest.raises(ValidationError) as caught:
        objective_of(team, level=L.INTERNAL_ST)
    assert caught.value.code == "level"


def test_a_year_of_another_thesis_is_refused(team: SimpleNamespace) -> None:
    other = make_team()
    with pytest.raises(ValidationError) as caught:
        objective_of(team, year=year_of(other))
    assert caught.value.code == "year"
    assert not Objective.objects.filter(thesis=team.thesis).exists()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("title", "", "title"),
        ("title", "   ", "title"),
        ("title", "x" * 201, "title"),
        ("title", "avec\x07cloche", "title"),
        ("description", "x" * 4001, "description"),
        ("success_criterion", "x" * 1001, "success_criterion"),
        ("weight", 0, "weight"),
        ("weight", 4, "weight"),
        ("category", "SECRET", "category"),
        ("due_date", dt.datetime(2027, 6, 30, 10, 0, tzinfo=dt.UTC), "due_date"),
    ],
)
def test_invalid_fields_are_refused(
    team: SimpleNamespace, field: str, value: Any, code: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        objective_of(team, **{field: value})
    assert caught.value.code == code
    assert not Objective.objects.exists()


def test_texts_are_stripped_and_nfc_normalized(team: SimpleNamespace) -> None:
    """A decomposed é (e + U+0301) is stored composed; outer blanks go, inner newlines stay."""
    objective = objective_of(team, title="  E\u0301tude fictive \n", description="Ligne 1\nLigne 2")
    assert texts(team.student, objective)["title"] == "\u00c9tude fictive"
    assert texts(team.student, objective)["description"] == "Ligne 1\nLigne 2"


def test_a_finished_thesis_freezes_its_objectives(team: SimpleNamespace) -> None:
    objective = objective_of(team)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.DEFENDED)
    with pytest.raises(PermissionDenied):  # M13a: the status gate of can()
        objective_of(team)
    with pytest.raises(PermissionDenied):
        _revise(team, objective, title="Nouveau titre fictif")


# =========================================================================== draft edits


def test_a_draft_is_edited_freely_without_revision(team: SimpleNamespace) -> None:
    objective = objective_of(team)
    revisions = _revise(team, objective, title="Titre corrigé fictif", weight=3)
    assert revisions == []
    assert not ObjectiveRevision.objects.exists()
    assert texts(team.student, objective)["title"] == "Titre corrigé fictif"
    assert Objective.objects.get(pk=objective.pk).weight == 3
    (event,) = _events("OBJECTIVE_UPDATED")
    assert event.details_json == {"fields": ["title", "weight"]}


def test_an_unchanged_value_is_not_a_change(team: SimpleNamespace) -> None:
    objective = approved(team)
    assert _revise(team, objective, reason="", title="Caractériser une fuite fictive") == []
    assert not ObjectiveRevision.objects.exists()
    assert not Objective.objects.get(pk=objective.pk).changed_since_approval


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor", "csi"])
def test_only_the_student_edits(team: SimpleNamespace, role: str) -> None:
    objective = objective_of(team)
    with pytest.raises(PermissionDenied):
        services.update_objective(
            objective=objective, user=getattr(team, role), changes={"weight": 3}, reason=""
        )
    assert Objective.objects.get(pk=objective.pk).weight == 2


def test_unknown_or_read_only_fields_are_refused(team: SimpleNamespace) -> None:
    objective = objective_of(team)
    for field in ("status", "progress", "approved_by", "thesis", "colour"):
        with pytest.raises(ValueError, match="cannot be changed"):
            _revise(team, objective, **{field: "x"})


# =========================================================================== approval


def test_the_director_approves_with_a_recent_second_factor(team: SimpleNamespace) -> None:
    objective = objective_of(team)
    services.approve_objective(objective=objective, user=team.director, recent_mfa=True)
    row = Objective.objects.get(pk=objective.pk)
    assert row.approved_by_id == team.director.pk
    assert row.approved_at == NOW
    (event,) = _events("OBJECTIVE_APPROVED")
    assert event.actor_id == team.director.pk


def test_approval_without_a_recent_second_factor_is_refused(team: SimpleNamespace) -> None:
    objective = objective_of(team)
    with pytest.raises(ReauthenticationRequiredError):
        services.approve_objective(objective=objective, user=team.director, recent_mfa=False)
    assert Objective.objects.get(pk=objective.pk).approved_at is None
    assert not _events("OBJECTIVE_APPROVED")


@pytest.mark.parametrize("role", ["student", "co_supervisor", "tutor", "csi", "guest"])
def test_only_the_director_approves(team: SimpleNamespace, role: str) -> None:
    objective = objective_of(team)
    with pytest.raises(PermissionDenied):
        services.approve_objective(objective=objective, user=getattr(team, role), recent_mfa=True)
    assert Objective.objects.get(pk=objective.pk).approved_at is None
    (event,) = _events("OBJECTIVE_ACCESS_DENIED")
    assert event.details_json["operation"] == "approve"


def test_approving_twice_or_a_dropped_objective_is_refused(team: SimpleNamespace) -> None:
    objective = approved(team)
    with pytest.raises(ValidationError) as caught:
        services.approve_objective(objective=objective, user=team.director, recent_mfa=True)
    assert caught.value.code == "already_approved"
    dropped = objective_of(team)
    services.drop_objective(objective=dropped, user=team.student, reason="Abandon fictif motivé")
    with pytest.raises(ValidationError) as caught:
        services.approve_objective(objective=dropped, user=team.director, recent_mfa=True)
    assert caught.value.code == "dropped"


def test_approve_a_whole_year(team: SimpleNamespace) -> None:
    first, second = objective_of(team), objective_of(team)
    other_year = objective_of(team, year=year_of(team, 2))
    count = services.approve_year(year=year_of(team), user=team.director, recent_mfa=True)
    assert count == 2
    assert all(
        Objective.objects.get(pk=o.pk).approved_by_id == team.director.pk for o in (first, second)
    )
    assert Objective.objects.get(pk=other_year.pk).approved_at is None
    assert len(_events("OBJECTIVE_APPROVED")) == 2


# =========================================================================== revisions


def test_editing_an_approved_objective_without_reason_is_refused(team: SimpleNamespace) -> None:
    """Acceptance criterion 1 (service side)."""
    objective = approved(team)
    for reason in ("", "   ", "court"):  # empty, blank, under 10 characters
        with pytest.raises(ValidationError) as caught:
            _revise(team, objective, reason=reason, title="Titre modifié fictif")
        assert caught.value.code == "reason_required"
    row = Objective.objects.get(pk=objective.pk)
    assert texts(team.student, row)["title"] == "Caractériser une fuite fictive"
    assert not row.changed_since_approval
    assert not ObjectiveRevision.objects.exists()
    assert not _events("OBJECTIVE_REVISED")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "Titre modifié fictif"),
        ("description", "Description modifiée"),
        ("success_criterion", "Critère modifié"),
        ("due_date", D(2027, 9, 30)),
        ("weight", 3),
        ("category", Category.PUBLICATION),
        ("level", L.CONFIDENTIAL),
    ],
)
def test_every_definition_field_needs_a_reason_once_approved(
    team: SimpleNamespace, field: str, value: Any
) -> None:
    objective = approved(team)
    with pytest.raises(ValidationError):
        _revise(team, objective, **{field: value})
    assert not ObjectiveRevision.objects.exists()


def test_a_revision_keeps_old_and_new_values_sealed(team: SimpleNamespace) -> None:
    objective = approved(team)
    reason = "Le périmètre fictif a changé après la réunion."
    revisions = _revise(
        team, objective, reason=reason, title="Nouveau titre fictif", due_date=D(2027, 9, 30)
    )
    assert [r.field for r in revisions] == [RevisedField.TITLE, RevisedField.DUE_DATE]
    views = services.revisions_for(team.director, objective)
    by_field = {v.revision.field: v for v in views}
    title = by_field[RevisedField.TITLE]
    assert (title.old_value, title.new_value, title.reason) == (
        "Caractériser une fuite fictive",
        "Nouveau titre fictif",
        reason,
    )
    due = by_field[RevisedField.DUE_DATE]
    assert (due.old_value, due.new_value, due.reason) == ("2027-06-30", "2027-09-30", reason)
    assert due.revision.changed_by_id == team.student.pk
    # Text values are sealed in their OWN slots (old in 0x0105, new in 0x0106).
    title_rev = title.revision
    sealed = dict(SealedText.objects.filter(object_id=title_rev.pk).values_list("slot", "level"))
    assert sealed == {0x0104: "PUBLIC", 0x0105: "PUBLIC", 0x0106: "PUBLIC"}
    # Non-text values are kept in clear: no sealed old or new value.
    assert set(
        SealedText.objects.filter(object_id=due.revision.pk).values_list("slot", flat=True)
    ) == {0x0104}
    assert texts(team.student, objective)["title"] == "Nouveau titre fictif"
    row = Objective.objects.get(pk=objective.pk)
    assert row.due_date == D(2027, 9, 30)
    assert row.changed_since_approval
    assert row.approved_at is not None  # still approved
    (event,) = _events("OBJECTIVE_REVISED")
    assert event.details_json == {"fields": ["title", "due_date"], "revisions": 2}


def test_old_and_new_values_are_not_swapped(team: SimpleNamespace) -> None:
    """Each revision text, opened through its own slot, gives back its own value."""
    objective = approved(team, description="Ancienne description")
    _revise(team, objective, reason="Motif fictif suffisant", description="Nouvelle description")
    (revision,) = _revisions(objective)
    opened = vault.open_texts(
        team.student,
        team.thesis,
        [(revision, "old_value"), (revision, "new_value"), (revision, "reason")],
        action=Action.OBJECTIVE_READ,
    )
    assert opened == {
        (revision.pk, "old_value"): "Ancienne description",
        (revision.pk, "new_value"): "Nouvelle description",
        (revision.pk, "reason"): "Motif fictif suffisant",
    }


def test_clearing_a_text_is_a_revision_with_an_empty_new_value(team: SimpleNamespace) -> None:
    objective = approved(team)
    _revise(team, objective, reason="Description retirée, motif fictif", description="")
    (view,) = services.revisions_for(team.student, objective)
    assert (view.old_value, view.new_value) == ("Description fictive.", "")
    assert texts(team.student, objective)["description"] == ""


def test_a_level_change_reseals_every_text_of_the_objective(team: SimpleNamespace) -> None:
    objective = approved(team)
    services.add_comment(objective=objective, user=team.director, body="Commentaire fictif")
    services.add_progress(
        objective=objective,
        user=team.student,
        date=D(2027, 3, 1),
        progress=20,
        status=Status.IN_PROGRESS,
        note="Note fictive",
    )
    _revise(team, objective, reason="Résultat ST fictif en jeu", level=L.CONFIDENTIAL)
    levels = set(SealedText.objects.filter(thesis=team.thesis).values_list("level", flat=True))
    assert levels == {"CONFIDENTIAL"}
    assert Objective.objects.get(pk=objective.pk).level == L.CONFIDENTIAL
    assert texts(team.csi, objective)["title"] is vault.RESTRICTED
    assert texts(team.director, objective)["title"] == "Caractériser une fuite fictive"
    (revision,) = _revisions(objective)
    assert (revision.field, revision.old_clear, revision.new_clear) == (
        RevisedField.LEVEL,
        "PUBLIC",
        "CONFIDENTIAL",
    )


# =========================================================================== acknowledgement


def test_the_director_acknowledges_changes_with_a_recent_second_factor(
    team: SimpleNamespace,
) -> None:
    objective = approved(team)
    _revise(team, objective, reason="Motif fictif suffisant", weight=3)
    services.acknowledge_changes(objective=objective, user=team.director, recent_mfa=True)
    row = Objective.objects.get(pk=objective.pk)
    assert not row.changed_since_approval
    assert (row.acknowledged_by_id, row.acknowledged_at) == (team.director.pk, NOW)
    (event,) = _events("OBJECTIVE_CHANGES_ACKNOWLEDGED")
    assert event.actor_id == team.director.pk
    assert event.details_json == {"revisions": 1, "result": False}


def test_acknowledging_without_a_recent_second_factor_is_refused(team: SimpleNamespace) -> None:
    objective = approved(team)
    _revise(team, objective, reason="Motif fictif suffisant", weight=3)
    with pytest.raises(ReauthenticationRequiredError):
        services.acknowledge_changes(objective=objective, user=team.director, recent_mfa=False)
    assert Objective.objects.get(pk=objective.pk).changed_since_approval
    assert not _events("OBJECTIVE_CHANGES_ACKNOWLEDGED")


@pytest.mark.parametrize("role", ["student", "co_supervisor", "tutor", "csi"])
def test_only_the_director_acknowledges(team: SimpleNamespace, role: str) -> None:
    objective = approved(team)
    _revise(team, objective, reason="Motif fictif suffisant", weight=3)
    with pytest.raises(PermissionDenied):
        services.acknowledge_changes(objective=objective, user=getattr(team, role), recent_mfa=True)
    assert Objective.objects.get(pk=objective.pk).changed_since_approval


def test_nothing_to_acknowledge(team: SimpleNamespace) -> None:
    objective = approved(team)
    with pytest.raises(ValidationError) as caught:
        services.acknowledge_changes(objective=objective, user=team.director, recent_mfa=True)
    assert caught.value.code == "nothing_to_acknowledge"


def test_a_new_change_after_acknowledgement_flags_it_again(team: SimpleNamespace) -> None:
    objective = approved(team)
    _revise(team, objective, reason="Motif fictif suffisant", weight=3)
    services.acknowledge_changes(objective=objective, user=team.director, recent_mfa=True)
    _revise(team, objective, reason="Second motif fictif", weight=1)
    assert Objective.objects.get(pk=objective.pk).changed_since_approval
    # Same frozen instant as the first acknowledgement: the count is still exact.
    services.acknowledge_changes(objective=objective, user=team.director, recent_mfa=True)
    counts = [e.details_json["revisions"] for e in _events("OBJECTIVE_CHANGES_ACKNOWLEDGED")]
    assert counts == [1, 1]
    assert not ObjectiveRevision.objects.filter(acknowledged_at__isnull=True).exists()


# =========================================================================== drop


def test_dropping_needs_a_reason_and_keeps_the_objective(team: SimpleNamespace) -> None:
    objective = approved(team)
    with pytest.raises(ValidationError) as caught:
        services.drop_objective(objective=objective, user=team.student, reason="")
    assert caught.value.code == "reason_required"
    services.drop_objective(
        objective=objective, user=team.student, reason="Piste abandonnée, motif fictif"
    )
    row = Objective.objects.get(pk=objective.pk)  # still there
    assert row.status == Status.DROPPED
    assert row.changed_since_approval
    (view,) = services.revisions_for(team.csi, row)
    assert (view.revision.field, view.revision.old_clear, view.revision.new_clear) == (
        RevisedField.STATUS,
        "PLANNED",
        "DROPPED",
    )
    assert view.reason == "Piste abandonnée, motif fictif"
    (event,) = _events("OBJECTIVE_DROPPED")
    assert event.details_json == {"previous_status": "PLANNED"}


def test_a_dropped_objective_cannot_be_dropped_edited_or_progressed(
    team: SimpleNamespace,
) -> None:
    objective = objective_of(team)
    services.drop_objective(objective=objective, user=team.student, reason="Motif fictif suffisant")
    for call in (
        lambda: services.drop_objective(
            objective=objective, user=team.student, reason="Motif fictif suffisant"
        ),
        lambda: _revise(team, objective, reason="Motif fictif suffisant", weight=3),
        lambda: services.add_progress(
            objective=objective,
            user=team.student,
            date=D(2027, 3, 1),
            progress=10,
            status=Status.IN_PROGRESS,
        ),
    ):
        with pytest.raises(ValidationError) as caught:
            call()
        assert caught.value.code == "dropped"


# =========================================================================== progress


def test_progress_updates_the_objective(team: SimpleNamespace) -> None:
    objective = approved(team)
    update = services.add_progress(
        objective=objective,
        user=team.student,
        date=D(2027, 3, 10),
        progress=40,
        status=Status.IN_PROGRESS,
        note="Premières mesures fictives",
    )
    row = Objective.objects.get(pk=objective.pk)
    assert (row.progress, row.status) == (40, Status.IN_PROGRESS)
    assert not row.changed_since_approval  # progress is not a definition change
    (view,) = services.progress_for(team.director, objective)
    assert (view.update.pk, view.note, view.evidence) == (
        update.pk,
        "Premières mesures fictives",
        None,
    )
    (event,) = _events("OBJECTIVE_PROGRESS_ADDED")
    assert event.details_json == {
        "progress": 40,
        "status": "IN_PROGRESS",
        "evidence": False,
        "result_declared": False,
    }


def test_achieved_means_100(team: SimpleNamespace) -> None:
    objective = approved(team)
    services.add_progress(
        objective=objective,
        user=team.student,
        date=D(2027, 3, 10),
        progress=80,
        status=Status.ACHIEVED,
    )
    assert Objective.objects.get(pk=objective.pk).progress == 100


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"progress": 101}, "progress"),
        ({"progress": -1}, "progress"),
        ({"status": Status.DROPPED}, "status"),  # dropping has its own service and reason
        ({"date": D(2027, 3, 16)}, "date"),  # tomorrow in Paris
        ({"date": dt.datetime(2027, 3, 10, 9, 0, tzinfo=dt.UTC)}, "date"),
    ],
)
def test_invalid_progress_is_refused(
    team: SimpleNamespace, kwargs: dict[str, Any], code: str
) -> None:
    objective = approved(team)
    fields: dict[str, Any] = {"date": D(2027, 3, 10), "progress": 30, "status": Status.IN_PROGRESS}
    fields.update(kwargs)
    with pytest.raises(ValidationError) as caught:
        services.add_progress(objective=objective, user=team.student, **fields)
    assert caught.value.code == code
    assert not ProgressUpdate.objects.exists()


def test_today_in_paris_is_not_the_future(team: SimpleNamespace, time_machine: Any) -> None:
    """At 23:30 UTC on 14 March it is already 15 March in Paris."""
    objective = approved(team)
    time_machine.move_to(dt.datetime(2027, 3, 14, 23, 30, tzinfo=dt.UTC), tick=False)
    services.add_progress(
        objective=objective,
        user=team.student,
        date=D(2027, 3, 15),
        progress=10,
        status=Status.IN_PROGRESS,
    )


def _document(team: SimpleNamespace, level: L, thesis: Any = None, user: Any = None) -> Any:
    with time_machine.travel(NOW, tick=False):
        return vault.store(
            thesis=thesis or team.thesis,
            upload=SimpleUploadedFile("preuve.pdf", b"%PDF-1.7\n%preuve fictive\n%%EOF\n"),
            title="Preuve fictive",
            level=level,
            user=user or team.student,
        )


def test_evidence_is_shown_only_to_who_may_read_it(team: SimpleNamespace) -> None:
    objective = approved(team)
    document = _document(team, L.INTERNAL_ST)
    services.add_progress(
        objective=objective,
        user=team.student,
        date=D(2027, 3, 10),
        progress=50,
        status=Status.IN_PROGRESS,
        evidence=document,
    )
    (for_tutor,) = services.progress_for(team.tutor, objective)
    (for_director,) = services.progress_for(team.director, objective)
    assert for_tutor.evidence == document
    assert for_director.evidence is None  # INTERNAL_ST: not the director's
    assert for_director.has_hidden_evidence


def test_evidence_of_another_thesis_is_refused(team: SimpleNamespace) -> None:
    other = make_team()
    document = _document(other, L.PUBLIC, thesis=other.thesis, user=other.student)
    with pytest.raises(ValidationError) as caught:
        services.add_progress(
            objective=approved(team),
            user=team.student,
            date=D(2027, 3, 10),
            progress=50,
            status=Status.IN_PROGRESS,
            evidence=document,
        )
    assert caught.value.code == "evidence"


# =========================================================================== comments


def test_supervisors_comment_and_the_csi_does_not_see_comments(team: SimpleNamespace) -> None:
    """Acceptance criterion 4 (service side): objectives read-only, no comments."""
    objective = approved(team)
    for author in (team.director, team.co_supervisor, team.tutor, team.student):
        services.add_comment(objective=objective, user=author, body=f"Avis fictif {author.pk}")
    bodies = [c.body for c in services.comments_for(team.director, objective)]
    assert len(bodies) == 4
    with pytest.raises(PermissionDenied):
        services.comments_for(team.csi, objective)
    with pytest.raises(PermissionDenied):
        services.add_comment(objective=objective, user=team.csi, body="Avis CSI fictif")
    assert ObjectiveComment.objects.count() == 4
    assert texts(team.csi, objective)["title"] == "Caractériser une fuite fictive"  # read-only
    with pytest.raises(PermissionDenied):
        services.update_objective(
            objective=objective, user=team.csi, changes={"weight": 1}, reason="Motif fictif long"
        )


def test_nobody_comments_an_objective_whose_level_they_cannot_read(
    team: SimpleNamespace,
) -> None:
    objective = approved(team, level=L.INTERNAL_LIRMM)
    with pytest.raises(PermissionDenied):
        services.add_comment(objective=objective, user=team.tutor, body="Avis fictif")
    assert not ObjectiveComment.objects.exists()


def test_a_comment_body_is_required_and_bounded(team: SimpleNamespace) -> None:
    objective = approved(team)
    for body in ("", "  ", "x" * 2001):
        with pytest.raises(ValidationError):
            services.add_comment(objective=objective, user=team.director, body=body)


# =========================================================================== years


def test_a_surplus_year_referenced_by_an_objective_is_kept(team: SimpleNamespace) -> None:
    """The case deferred from the years step: PROTECT keeps year 4."""
    Thesis.objects.filter(pk=team.thesis.pk).update(planned_end_date=D(2030, 6, 30))
    services.sync_years(team.thesis)
    objective = objective_of(team, year=year_of(team, 4))
    Thesis.objects.filter(pk=team.thesis.pk).update(planned_end_date=D(2029, 11, 30))
    result = services.sync_years(team.thesis)
    assert result == services.SyncResult(created=0, updated=0, deleted=0, kept=1)
    assert ThesisYear.objects.filter(thesis=team.thesis, number=4).exists()
    assert Objective.objects.get(pk=objective.pk).year.number == 4


# =========================================================================== IDOR, audit hygiene


def test_an_objective_of_another_thesis_through_my_thesis_is_refused(team: SimpleNamespace) -> None:
    other = make_team()
    theirs = objective_of(other, user=other.student, year=year_of(other))
    with pytest.raises(PermissionDenied):
        services.objective_for(team.director, team.thesis, theirs.pk, operation="read")
    with pytest.raises(PermissionDenied):
        services.objective_for(team.director, team.thesis, "not-a-uuid", operation="read")
    assert (
        services.objective_for(other.director, other.thesis, theirs.pk, operation="read") == theirs
    )


def test_no_title_reason_note_or_comment_reaches_the_audit_log_or_clear_columns(
    team: SimpleNamespace,
) -> None:
    marks = {
        name: marker() for name in ("title", "desc", "crit", "reason", "note", "comment", "drop")
    }
    objective = approved(
        team, title=marks["title"], description=marks["desc"], success_criterion=marks["crit"]
    )
    _revise(team, objective, reason=f"{marks['reason']} motif", title=f"{marks['title']} bis")
    services.add_progress(
        objective=objective,
        user=team.student,
        date=D(2027, 3, 1),
        progress=5,
        status=Status.IN_PROGRESS,
        note=marks["note"],
    )
    services.add_comment(objective=objective, user=team.director, body=marks["comment"])
    services.acknowledge_changes(objective=objective, user=team.director, recent_mfa=True)
    services.objective_texts(team.director, team.thesis, [objective])
    services.drop_objective(objective=objective, user=team.student, reason=f"{marks['drop']} fin")
    with connection.cursor() as cursor:
        dumps = []
        for query in (
            "SELECT row_to_json(t)::text FROM vault_auditevent t",
            "SELECT row_to_json(t)::text FROM vault_sealedtext t",
            "SELECT row_to_json(t)::text FROM objectives_objective t",
            "SELECT row_to_json(t)::text FROM objectives_objectiverevision t",
            "SELECT row_to_json(t)::text FROM objectives_progressupdate t",
            "SELECT row_to_json(t)::text FROM objectives_objectivecomment t",
        ):
            cursor.execute(query)
            dumps += [row[0] for row in cursor.fetchall()]
    assert len(dumps) > 10
    for dump in dumps:
        for mark in marks.values():
            assert mark not in dump


def test_every_sealed_text_belongs_to_a_known_slot_and_level(team: SimpleNamespace) -> None:
    objective = approved(team, level=L.INTERNAL_LIRMM)
    _revise(team, objective, reason="Motif fictif suffisant", title="Titre bis fictif")
    services.add_comment(objective=objective, user=team.director, body="Avis fictif")
    for slot, level in SealedText.objects.values_list("slot", "level"):
        assert formats.SLOT_NAMES[slot].startswith("objectives.")
        assert level == "INTERNAL_LIRMM"


# =========================================================================== declared results


def _report(team: SimpleNamespace, objective: Objective, status: str, progress: int = 60) -> None:
    services.add_progress(
        objective=objective,
        user=team.student,
        date=D(2027, 3, 10),
        progress=progress,
        status=status,
    )


@pytest.mark.parametrize("result", [Status.ACHIEVED, Status.PARTIAL, Status.NOT_ACHIEVED])
def test_a_declared_result_counts_at_once_and_awaits_acknowledgement(
    team: SimpleNamespace, result: Status
) -> None:
    objective = approved(team)
    _report(team, objective, result)
    row = Objective.objects.get(pk=objective.pk)
    assert row.status == result  # counted in the statistics straight away
    assert row.result_declared
    assert not row.changed_since_approval  # a result is not a change of definition
    (event,) = _events("OBJECTIVE_PROGRESS_ADDED")
    assert event.details_json["result_declared"] is True


def test_progress_without_a_result_sets_no_mark(team: SimpleNamespace) -> None:
    objective = approved(team)
    _report(team, objective, Status.IN_PROGRESS)
    assert not Objective.objects.get(pk=objective.pk).result_declared
    (event,) = _events("OBJECTIVE_PROGRESS_ADDED")
    assert event.details_json["result_declared"] is False


def test_a_draft_result_needs_no_acknowledgement(team: SimpleNamespace) -> None:
    objective = objective_of(team)
    _report(team, objective, Status.ACHIEVED)
    assert not Objective.objects.get(pk=objective.pk).result_declared


def test_withdrawing_a_result_is_a_change_of_result(team: SimpleNamespace) -> None:
    objective = approved(team)
    _report(team, objective, Status.ACHIEVED)
    services.acknowledge_changes(objective=objective, user=team.director, recent_mfa=True)
    _report(team, objective, Status.IN_PROGRESS)
    assert Objective.objects.get(pk=objective.pk).result_declared


def test_the_same_result_reported_again_is_not_a_change(team: SimpleNamespace) -> None:
    objective = approved(team)
    _report(team, objective, Status.PARTIAL, progress=50)
    services.acknowledge_changes(objective=objective, user=team.director, recent_mfa=True)
    _report(team, objective, Status.PARTIAL, progress=70)
    assert not Objective.objects.get(pk=objective.pk).result_declared


def test_the_director_acknowledges_a_declared_result(team: SimpleNamespace) -> None:
    objective = approved(team)
    _report(team, objective, Status.ACHIEVED)
    with pytest.raises(ReauthenticationRequiredError):
        services.acknowledge_changes(objective=objective, user=team.director, recent_mfa=False)
    with pytest.raises(PermissionDenied):
        services.acknowledge_changes(objective=objective, user=team.tutor, recent_mfa=True)
    assert Objective.objects.get(pk=objective.pk).result_declared
    services.acknowledge_changes(objective=objective, user=team.director, recent_mfa=True)
    row = Objective.objects.get(pk=objective.pk)
    assert not row.result_declared
    assert row.status == Status.ACHIEVED  # acknowledging does not change the result
    (event,) = _events("OBJECTIVE_CHANGES_ACKNOWLEDGED")
    assert event.details_json == {"revisions": 0, "result": True}


def test_one_acknowledgement_clears_both_marks(team: SimpleNamespace) -> None:
    objective = approved(team)
    _revise(team, objective, reason="Motif fictif suffisant", weight=3)
    _report(team, objective, Status.PARTIAL)
    services.acknowledge_changes(objective=objective, user=team.director, recent_mfa=True)
    row = Objective.objects.get(pk=objective.pk)
    assert not row.result_declared and not row.changed_since_approval
    (event,) = _events("OBJECTIVE_CHANGES_ACKNOWLEDGED")
    assert event.details_json == {"revisions": 1, "result": True}


def test_pending_acknowledgements_for_the_weekly_summary(team: SimpleNamespace) -> None:
    """What the director's summary will list (M4 weekly summary)."""
    changed, declared, quiet = approved(team), approved(team), approved(team)
    _revise(team, changed, reason="Motif fictif suffisant", weight=3)
    _report(team, declared, Status.ACHIEVED)
    _report(team, quiet, Status.IN_PROGRESS)
    pending = services.pending_acknowledgements(team.thesis)
    assert {o.pk for o in pending} == {changed.pk, declared.pk}
