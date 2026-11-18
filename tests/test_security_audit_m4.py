"""M4 security audit: one test (or a pointer to one) per lead of the end-of-module review.

Leads already closed elsewhere:
- 1 transplants and strict padding: apps/vault/tests/test_sealed_text.py
  ::test_ciphertext_moved_to_another_{object,field,thesis}_is_refused,
  ::test_content_ad_alone_binds_..., ::test_wrap_ad_alone_binds_...,
  ::test_altered_padding_is_refused_and_audited; apps/vault/tests/test_text_formats.py
  ::test_malformed_padding_is_an_integrity_failure. (Clear text: lead 1 below.)
- 2 levels: apps/vault/tests/test_sealed_text.py
  ::test_levels_a_role_cannot_read_come_back_restricted (tutor, co-supervisor, CSI);
  apps/objectives/tests/test_views.py::test_an_objective_at_an_unreadable_level_does_not_exist_for_the_reader,
  ::test_internal_st_is_not_offered (forced by POST: 400);
  apps/objectives/tests/test_services.py::test_internal_st_is_not_an_objective_level.
- 3 CSI and comments: apps/objectives/tests/test_views.py
  ::test_csi_sees_objectives_but_never_the_comments (page and fragment);
  apps/objectives/tests/test_services.py::test_supervisors_comment_and_the_csi_does_not_see_comments;
  apps/activity/tests/test_reads.py::test_a_comment_needs_the_comment_right_and_the_level
  (never the CSI member), and every activity e-mail is a link only
  (apps/activity/tests/test_m11b_digest_delivery.py::test_the_e_mail_is_a_link_only).
- 4 revisions: apps/objectives/tests/test_services.py
  ::test_editing_an_approved_objective_without_reason_is_refused,
  ::test_every_definition_field_needs_a_reason_once_approved,
  ::test_a_revision_keeps_old_and_new_values_sealed (one revision per field),
  ::test_a_declared_result_counts_at_once_and_awaits_acknowledgement,
  ::test_acknowledging_without_a_recent_second_factor_is_refused;
  apps/objectives/tests/test_views.py::test_editing_an_approved_objective_without_reason_is_refused,
  ::test_dropping_without_reason_is_refused, ::test_the_password_alone_is_not_enough.
- 6 evidence (service): apps/objectives/tests/test_services.py
  ::test_evidence_of_another_thesis_is_refused, ::test_evidence_is_shown_only_to_who_may_read_it.
- 7 statistics (results): apps/objectives/tests/test_stats.py (exact Fractions, PARTIAL,
  weights, empty denominators, DROPPED apart).
- 8 level change re-seals every dependent text: apps/objectives/tests/test_services.py
  ::test_a_level_change_reseals_every_text_of_the_objective; rewrap and retirement:
  apps/vault/tests/test_sealed_text.py::test_rotation_rewraps_texts_and_retirement_waits_for_them.
- 9 double approval answers 409:
  apps/objectives/tests/test_views.py::test_a_state_conflict_is_a_409.
- 10 time: apps/objectives/tests/test_years.py (29 February, September),
  test_sync_years.py::test_current_*_follows_the_date_in_paris (midnight, both time changes),
  test_stats_service.py::test_overdue_follows_the_date_in_paris,
  apps/activity/tests/test_m11b_digest_delivery.py
  ::test_the_weekly_digest_runs_on_the_monday_of_the_date_in_paris,
  test_services.py::test_today_in_paris_is_not_the_future.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
import threading
from io import StringIO
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.permissions import Action
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.accounts.tests.helpers import mfa_login
from apps.activity import digests
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.objectives import services
from apps.objectives.models import Objective, ObjectiveComment, ProgressUpdate, Status
from apps.objectives.tests.helpers import D
from apps.vault import services as vault
from apps.vault import storage
from apps.vault.models import AuditEvent, SealedText
from apps.vault.tests.concurrency import run_threads

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = secrets.token_urlsafe(16)
HX = {"HX-Request": "true"}
NOW = dt.datetime(2027, 3, 15, 10, 0, tzinfo=dt.UTC)


def _marker() -> str:
    return "TEMOIN-" + secrets.token_hex(8)


def _team() -> SimpleNamespace:
    thesis = ThesisFactory.create(
        cifre_start_date=D(2026, 12, 1),
        first_enrollment_date=D(2026, 12, 15),
        planned_end_date=D(2029, 11, 30),
    )

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(
            thesis=thesis, role=role, user__password=PASSWORD, **kwargs
        ).user

    return SimpleNamespace(
        thesis=thesis,
        student=member(Role.PHD_STUDENT),
        director=member(Role.DIRECTOR),
        co_supervisor=member(Role.CO_SUPERVISOR),
        tutor=member(Role.INDUSTRIAL_TUTOR),
        csi=member(Role.CSI_MEMBER, active_until=timezone.now() + dt.timedelta(days=60)),
    )


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(NOW, tick=True)
    return _team()


def _objective(team: SimpleNamespace, **overrides: Any) -> Objective:
    fields: dict[str, Any] = {
        "thesis": team.thesis,
        "user": team.student,
        "year": team.thesis.years.get(number=1),
        "title": "Titre fictif",
        "category": "RESEARCH",
        "due_date": D(2027, 6, 30),
        "level": L.PUBLIC,
    }
    fields.update(overrides)
    return services.create_objective(**fields)


def _url(thesis: Any, name: str, **kw: Any) -> str:
    return reverse(f"objectives:{name}", kwargs={"thesis_id": thesis.pk, **kw})


# ---------------------------------------------------------------- 1. no clear text anywhere


def _all_tables_dump() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        tables = [row[0] for row in cursor.fetchall()]
        dumps = []
        for table in tables:
            # Names come from the catalogue itself, quoted: no user input here.
            cursor.execute(
                'SELECT row_to_json(t)::text FROM "' + table.replace('"', '""') + '" t'  # noqa: S608  # nosec B608
            )
            dumps += [row[0] for row in cursor.fetchall()]
    return dumps


def test_no_marker_in_the_database_the_storage_the_logs_or_the_audit_log(
    team: SimpleNamespace, caplog: pytest.LogCaptureFixture, settings: Any
) -> None:
    caplog.set_level(logging.DEBUG)  # every logger, SQL included (parameters are never logged)
    marks = {k: _marker() for k in ("title", "desc", "crit", "reason", "note", "comment", "drop")}
    evidence = vault.store(
        thesis=team.thesis,
        upload=SimpleUploadedFile("p.pdf", b"%PDF-1.7\n%preuve fictive\n%%EOF\n"),
        title="Preuve fictive",
        level=L.CONFIDENTIAL,
        user=team.student,
    )
    student = Client()
    mfa_login(student, team.student, PASSWORD)
    student.post(
        _url(team.thesis, "new"),
        {
            "year": "1",
            "title": marks["title"],
            "description": marks["desc"],
            "success_criterion": marks["crit"],
            "category": "RESEARCH",
            "due_date": "2027-03-01",
            "weight": "2",
            "level": "CONFIDENTIAL",
        },
    )
    objective = Objective.objects.get()
    director = Client()
    mfa_login(director, team.director, PASSWORD)
    director.post(_url(team.thesis, "approve", objective_id=objective.pk))
    student.post(
        _url(team.thesis, "edit", objective_id=objective.pk),
        {
            "title": f"{marks['title']} bis",
            "description": marks["desc"],
            "success_criterion": marks["crit"],
            "category": "RESEARCH",
            "due_date": "2027-03-01",
            "weight": "3",
            "reason": f"{marks['reason']} motif",
        },
    )
    student.post(
        _url(team.thesis, "progress", objective_id=objective.pk),
        {
            "date": "2027-03-10",
            "progress": "40",
            "status": "IN_PROGRESS",
            "note": marks["note"],
            "evidence": str(evidence.pk),
        },
        headers=HX,
    )
    director.post(
        _url(team.thesis, "comments", objective_id=objective.pk),
        {"body": marks["comment"]},
        headers=HX,
    )
    director.get(_url(team.thesis, "dashboard"))
    director.get(_url(team.thesis, "detail", objective_id=objective.pk))
    director.post(_url(team.thesis, "acknowledge", objective_id=objective.pk))
    digests.run(D(2027, 3, 15))  # an overdue objective (due 1 March): its e-mail is sent
    student.post(
        _url(team.thesis, "drop", objective_id=objective.pk), {"reason": f"{marks['drop']} fin"}
    )
    assert ProgressUpdate.objects.count() == 1 and ObjectiveComment.objects.count() == 1
    assert Objective.objects.get().status == Status.DROPPED
    assert mail.outbox, "the overdue objective was e-mailed"

    dumps = _all_tables_dump()
    files = [p.read_bytes() for p in storage.root().rglob("*") if p.is_file()]
    logs = [f"{r.getMessage()} {r.args}" for r in caplog.records]
    mails = [m.subject + m.body for m in mail.outbox]
    assert len(dumps) > 50 and files and logs
    for mark in marks.values():
        for dump in dumps:
            assert mark not in dump
        for blob in files:
            assert mark.encode() not in blob
        for line in logs + mails:
            assert mark not in line
    # The texts are there, sealed: one row per sealed field, none in clear.
    assert SealedText.objects.filter(thesis=team.thesis).count() >= 8
    assert AuditEvent.objects.filter(action="SEALED_TEXT_READ").exists()


# --------------------------------------------------------------------------- 5. IDOR


def test_every_objective_route_refuses_an_objective_of_another_thesis(
    team: SimpleNamespace,
) -> None:
    other = _team()
    theirs = _objective(other, user=other.student, year=other.thesis.years.get(number=1))
    services.approve_objective(objective=theirs, user=other.director, recent_mfa=True)
    services.update_objective(
        objective=theirs, user=other.student, changes={"weight": 3}, reason="Motif fictif long"
    )
    before = Objective.objects.values_list(
        "weight", "level", "status", "changed_since_approval"
    ).get(pk=theirs.pk)
    counts = (ProgressUpdate.objects.count(), ObjectiveComment.objects.count())
    student, director = Client(), Client()
    mfa_login(student, team.student, PASSWORD)
    mfa_login(director, team.director, PASSWORD)
    calls = [
        (student, "get", "detail", {}),
        (student, "get", "edit", {}),
        (
            student,
            "post",
            "edit",
            {
                "title": "x",
                "category": "RESEARCH",
                "due_date": "2027-06-30",
                "weight": "1",
                "reason": "Motif fictif long",
            },
        ),
        (student, "post", "level", {"level": "PUBLIC", "reason": "Motif fictif long"}),
        (student, "post", "drop", {"reason": "Motif fictif long"}),
        (
            student,
            "post",
            "progress",
            {"date": "2027-03-10", "progress": "10", "status": "IN_PROGRESS"},
        ),
        (student, "get", "comments", {}),
        (director, "post", "comments", {"body": "Avis fictif"}),
        (director, "post", "approve", {}),
        (director, "post", "acknowledge", {}),
    ]
    for client, method, name, data in calls:
        response = getattr(client, method)(
            _url(team.thesis, name, objective_id=theirs.pk), data, headers=HX
        )
        assert response.status_code == 403, (name, method)
    after = Objective.objects.values_list(
        "weight", "level", "status", "changed_since_approval"
    ).get(pk=theirs.pk)
    assert after == before
    assert (ProgressUpdate.objects.count(), ObjectiveComment.objects.count()) == counts


def test_years_revisions_progress_and_comments_never_cross_theses(team: SimpleNamespace) -> None:
    """No URL carries their id; through the services, the thesis of the URL decides."""
    other = _team()
    theirs = _objective(other, user=other.student, year=other.thesis.years.get(number=1))
    services.approve_objective(objective=theirs, user=other.director, recent_mfa=True)
    services.update_objective(
        objective=theirs, user=other.student, changes={"title": "Autre"}, reason="Motif fictif long"
    )
    services.add_comment(objective=theirs, user=other.director, body="Avis fictif")
    revision = theirs.revisions.get()
    comment = theirs.comments.get()
    with pytest.raises(PermissionDenied):
        services.stats_for(team.director, team.thesis, other.thesis.years.get(number=1))
    for obj, name in ((revision, "reason"), (comment, "body"), (theirs, "title")):
        with pytest.raises(PermissionDenied):
            vault.open_texts(
                team.director, team.thesis, [(obj, name)], action=Action.OBJECTIVE_READ
            )
    response = Client()
    mfa_login(response, team.director, PASSWORD)
    # An unknown year number of MY thesis is the only thing approve-year accepts: 400.
    assert response.post(_url(team.thesis, "approve_year"), {"year": "9"}).status_code == 400
    assert Objective.objects.get(pk=theirs.pk).approved_by_id == other.director.pk


# --------------------------------------------------------------------------- 6. evidence (view)


def test_the_progress_form_refuses_a_document_of_another_thesis(team: SimpleNamespace) -> None:
    other = _team()
    foreign = vault.store(
        thesis=other.thesis,
        upload=SimpleUploadedFile("p.pdf", b"%PDF-1.7\n%autre\n%%EOF\n"),
        title="Document d'une autre thèse",
        level=L.PUBLIC,
        user=other.student,
    )
    objective = _objective(team)
    client = Client()
    client.force_login(team.student)
    page = client.get(_url(team.thesis, "detail", objective_id=objective.pk)).content.decode()
    assert str(foreign.pk) not in page  # not even offered
    response = client.post(
        _url(team.thesis, "progress", objective_id=objective.pk),
        {
            "date": "2027-03-10",
            "progress": "10",
            "status": "IN_PROGRESS",
            "evidence": str(foreign.pk),
        },
        headers=HX,
    )
    assert response.status_code == 400
    assert not ProgressUpdate.objects.exists()


# ---------------------------------------------------------------- 7. statistics open no text


def test_stats_for_opens_no_text(team: SimpleNamespace) -> None:
    objective = _objective(team, level=L.CONFIDENTIAL)
    services.approve_objective(objective=objective, user=team.director, recent_mfa=True)
    reads = AuditEvent.objects.filter(action="SEALED_TEXT_READ").count()
    with (
        mock.patch.object(vault, "open_texts", side_effect=AssertionError("a text was opened")),
        mock.patch.object(vault, "open_text", side_effect=AssertionError("a text was opened")),
    ):
        result = services.stats_for(team.tutor, team.thesis, objective.year)
    assert result.stats.counted == 1  # counted, with no text opened (SR-01: a reader of it)
    assert AuditEvent.objects.filter(action="SEALED_TEXT_READ").count() == reads


# --------------------------------------------------------------------------- 8. rotation end to end


def test_rotation_and_retirement_keep_objective_texts_readable(
    team: SimpleNamespace, time_machine: Any
) -> None:
    objective = _objective(team, level=L.CONFIDENTIAL, title="Titre confidentiel fictif")
    services.approve_objective(objective=objective, user=team.director, recent_mfa=True)
    services.add_comment(objective=objective, user=team.director, body="Avis confidentiel")
    call_command("rotate_kek", "CONFIDENTIAL", stdout=StringIO())
    time_machine.move_to(NOW + dt.timedelta(hours=1), tick=True)  # past VAULT_RETIRE_GRACE
    call_command("rotate_kek", "CONFIDENTIAL", "--retire", stdout=StringIO())
    kek_ids = set(SealedText.objects.values_list("kek_id", flat=True))
    assert kek_ids == {"env:CONFIDENTIAL:2"}
    texts = services.objective_texts(team.director, team.thesis, [objective])[objective.pk]
    assert texts["title"] == "Titre confidentiel fictif"
    assert [c.body for c in services.comments_for(team.tutor, objective)] == ["Avis confidentiel"]


# --------------------------------------------------------------------------- 9. concurrency


def test_a_concurrent_change_is_detected(team: SimpleNamespace, monkeypatch: Any) -> None:
    """Another request saves the objective between the read of its texts and the
    lock: the update is refused (stale, 409 in the view), nothing is written."""
    objective = _objective(team)
    services.approve_objective(objective=objective, user=team.director, recent_mfa=True)
    real = services._current_values

    def read_then_someone_else_writes(*args: Any, **kwargs: Any) -> Any:
        values = real(*args, **kwargs)
        Objective.objects.filter(pk=objective.pk).update(
            weight=3, updated_at=timezone.now() + dt.timedelta(seconds=1)
        )
        return values

    monkeypatch.setattr(services, "_current_values", read_then_someone_else_writes)
    with pytest.raises(ValidationError) as caught:
        services.update_objective(
            objective=objective,
            user=team.student,
            changes={"title": "Concurrent fictif"},
            reason="Motif fictif suffisant",
        )
    assert caught.value.code == "stale"
    assert not objective.revisions.exists()
    texts = services.objective_texts(team.student, team.thesis, [objective])[objective.pk]
    assert texts["title"] == "Titre fictif"
    client = Client()
    client.force_login(team.student)
    response = client.post(
        _url(team.thesis, "edit", objective_id=objective.pk),
        {
            "title": "Concurrent fictif",
            "category": "RESEARCH",
            "due_date": "2027-06-30",
            "weight": "1",
            "reason": "Motif fictif suffisant",
        },
    )
    assert response.status_code == 409


@pytest.mark.serial
def test_two_simultaneous_approvals_approve_once(team: SimpleNamespace) -> None:
    objective = _objective(team)
    barrier = threading.Barrier(2, timeout=5)
    outcomes: list[str] = []

    def approve() -> None:
        barrier.wait()
        try:
            services.approve_objective(objective=objective, user=team.director, recent_mfa=True)
            outcomes.append("approved")
        except ValidationError as exc:
            outcomes.append(str(exc.code))

    run_threads(approve, approve, control=lambda: None)
    assert sorted(outcomes) == ["already_approved", "approved"]
    assert AuditEvent.objects.filter(action="OBJECTIVE_APPROVED").count() == 1


# --------------------------------------------------------------------------- admin


def test_no_objective_model_in_the_django_admin() -> None:
    from django.apps import apps
    from django.contrib import admin

    registered = {m for m in admin.site._registry if m._meta.app_label == "objectives"}
    assert registered == set()
    assert SealedText not in admin.site._registry
    assert apps.get_app_config("objectives")  # the app is installed


def test_an_unknown_user_gets_nothing(team: SimpleNamespace) -> None:
    stranger = UserFactory.create(password=PASSWORD)
    objective = _objective(team)
    client = Client()
    client.force_login(stranger)
    for name in ("detail", "comments"):
        assert client.get(_url(team.thesis, name, objective_id=objective.pk)).status_code == 403
    assert client.get(_url(team.thesis, "dashboard")).status_code == 403
