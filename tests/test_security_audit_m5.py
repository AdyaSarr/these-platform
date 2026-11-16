"""M5a security audit: one test (or a pointer to one) per lead of the end-of-module review.

Leads already closed elsewhere:
- 2 levels and attendees: apps/meetings/tests/test_services.py
  ::test_every_attendee_reads_the_level, ::test_change_level_excluding_an_attendee_is_refused,
  ::test_a_non_attendee_supervisor_sees_the_meeting_and_texts_by_level,
  ::test_change_level_reseals_every_text; apps/meetings/tests/test_views.py
  ::test_restricted_content_for_a_reader_without_the_level.
- 3 prefilled agenda never widens a text: apps/meetings/tests/test_services.py
  ::test_prefill_copies_only_items_read_by_every_role_of_the_meeting.
- 4 second factor after the rights (403 first): apps/meetings/tests/test_views.py
  ::test_the_meeting_guard_comes_before_the_second_factor, ::test_approve,
  ::test_change_level; services: ::test_change_level_needs_a_recent_second_factor,
  ::test_acknowledgements.
- 6 e-mails a link only, ids through the broker, sent once, former members:
  apps/meetings/tests/test_notifications.py, apps/meetings/tests/test_former_attendees.py.
- 7 .ics: apps/meetings/tests/test_views.py::test_ics_carries_labels_never_texts,
  ::test_ics_of_a_cancelled_meeting, ::test_ics_is_refused_outside_the_team.
- 8 time: apps/meetings/tests/test_timing.py (both time changes, bounds, months),
  test_services.py::test_meeting_dates_are_bounded, ::test_overdue_is_decided_on_the_date_in_paris,
  test_notifications.py::test_the_reminder_goes_the_day_before_in_paris.
- 9 CSI minutes not stored in M5a: test_services.py::test_csi_minutes_are_not_available_in_m5a,
  ::test_a_meeting_with_minutes_cannot_become_a_csi;
  test_views.py::test_a_csi_meeting_has_no_minutes_yet.
- 10 POSTs like a browser: test_views.py::test_create_posted_like_a_browser_in_paris_time,
  ::test_minutes_posted_like_a_browser_then_stale, ::test_duplicate_view, ::test_approve.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
import threading
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.urls import get_resolver, reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.accounts.tests.helpers import mfa_login
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.meetings import reminders, services
from apps.meetings.models import (
    ActionItem,
    Decision,
    Meeting,
    MeetingDocument,
    MinutesAcknowledgement,
)
from apps.objectives import services as objectives
from apps.objectives.tests.helpers import D
from apps.vault import services as vault
from apps.vault import storage
from apps.vault.models import AuditEvent, SealedText
from apps.vault.tests.concurrency import run_threads

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = secrets.token_urlsafe(16)
START = dt.datetime(2027, 1, 12, 13, 0, tzinfo=dt.UTC)
NOW = START + dt.timedelta(hours=1)  # minutes, decisions and actions open after the start


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


def _meeting(team: SimpleNamespace, **overrides: Any) -> Meeting:
    fields: dict[str, Any] = {
        "thesis": team.thesis,
        "user": team.student,
        "kind": Meeting.Kind.SUPERVISION,
        "starts_at": START,
        "duration_minutes": 60,
        "location": Meeting.Location.VISIO,
        "level": L.PUBLIC,
        "title": "Titre fictif",
        "attendees": [team.student, team.director],
    }
    fields.update(overrides)
    return services.create_meeting(**fields)


def _url(thesis: Any, name: str, **kw: Any) -> str:
    return reverse(f"meetings:{name}", kwargs={"thesis_id": thesis.pk, **kw})


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


def test_no_marker_in_the_database_the_storage_the_logs_the_mails_or_the_ics(
    team: SimpleNamespace, caplog: pytest.LogCaptureFixture, time_machine: Any
) -> None:
    caplog.set_level(logging.DEBUG)  # every logger, SQL included (parameters are never logged)
    keys = ("title", "room", "link", "agenda", "minutes", "decision", "action", "source")
    marks = {k: _marker() for k in keys}
    source = _meeting(team, title="Source fictive", attendees=[team.student])
    services.add_action(
        meeting=source,
        user=team.student,
        title=marks["source"],
        assignee=team.student,
        due_date=D(2027, 1, 20),
    )
    document = vault.store(
        thesis=team.thesis,
        upload=SimpleUploadedFile("p.pdf", b"%PDF-1.7\n%annexe fictive\n%%EOF\n"),
        title="Annexe fictive",
        level=L.CONFIDENTIAL,
        user=team.student,
    )
    student, director = Client(), Client()
    mfa_login(student, team.student, PASSWORD)
    mfa_login(director, team.director, PASSWORD)
    response = student.post(
        _url(team.thesis, "new"),
        {
            "kind": "TRIPARTITE",
            "title": marks["title"],
            "date": "2027-01-12",
            "time": "14:00",
            "duration_minutes": "60",
            "location": "OTHER",
            "location_detail": marks["room"],
            "visio_link": f"https://visio.example.org/{marks['link']}",
            "level": "CONFIDENTIAL",
            "attendees": [str(team.student.pk), str(team.director.pk), str(team.tutor.pk)],
        },
    )
    assert response.status_code == 302, response.content[:500]
    meeting = Meeting.objects.get(kind="TRIPARTITE")
    m = {"meeting_id": meeting.pk}
    student.post(_url(team.thesis, "agenda", **m), {"agenda": marks["agenda"]})
    student.post(_url(team.thesis, "prefill", **m))
    student.post(_url(team.thesis, "minutes", **m), {"minutes": marks["minutes"], "version": "0"})
    director.post(_url(team.thesis, "decision", **m), {"text": marks["decision"]})
    director.post(
        _url(team.thesis, "action", **m),
        {
            "title": marks["action"],
            "assignee": str(team.tutor.pk),
            "due_date": "2027-01-15",
            "objective": "",
        },
    )
    student.post(_url(team.thesis, "document", **m), {"document": str(document.pk)})
    director.post(_url(team.thesis, "approve", **m), {"version": "1"})
    ics = director.get(_url(team.thesis, "ics", **m)).content.decode()
    for page in ("detail", "duplicate"):
        director.get(_url(team.thesis, page, **m))
    director.get(_url(team.thesis, "list"))
    director.get(_url(team.thesis, "register"))
    director.get(reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk}))
    # The eve's reminder is sent on the eve; rule still_due 5.7.
    time_machine.move_to(dt.datetime(2027, 1, 11, 8, 0, tzinfo=dt.UTC), tick=True)
    reminders.run(D(2027, 1, 11))  # the day before: reminders
    time_machine.move_to(NOW, tick=True)
    reminders.run(D(2027, 1, 16))  # J+1 of the action: overdue e-mail
    assert MinutesAcknowledgement.objects.filter(kind="APPROVED").count() == 1
    assert Decision.objects.count() == 1 and ActionItem.objects.count() == 2
    assert MeetingDocument.objects.count() == 1
    assert {m.subject for m in mail.outbox} >= {
        "Thèse CIFRE\u00a0: une réunion a lieu demain",
        "Thèse CIFRE\u00a0: une action est en retard",
        "Thèse CIFRE\u00a0: un compte rendu à lire",
    }

    dumps = _all_tables_dump()
    files = [p.read_bytes() for p in storage.root().rglob("*") if p.is_file()]
    logs = [f"{r.getMessage()} {r.args}" for r in caplog.records]
    mails = [f"{m.subject}{m.body}" for m in mail.outbox]
    assert len(dumps) > 50 and files and logs and mails
    for mark in marks.values():
        for dump in dumps:
            assert mark not in dump
        for blob in files:
            assert mark.encode() not in blob
        for line in [*logs, *mails, ics]:
            assert mark not in line
    # The texts are there, sealed, at the meeting's level.
    rows = SealedText.objects.filter(object_id=meeting.pk)
    assert set(rows.values_list("level", flat=True)) == {"CONFIDENTIAL"}
    assert rows.count() == 5  # title, room, link, agenda, minutes


# --------------------------------------------------------------------------- 5. IDOR


def test_every_meeting_route_refuses_a_meeting_of_another_thesis(team: SimpleNamespace) -> None:
    other = _team()
    theirs = _meeting(other, user=other.student, attendees=[other.student, other.director])
    services.save_minutes(meeting=theirs, user=other.student, minutes="CR fictif", version=0)
    decision = services.add_decision(meeting=theirs, user=other.student, text="Décision fictive")
    action = services.add_action(
        meeting=theirs,
        user=other.student,
        title="Action fictive",
        assignee=other.student,
        due_date=D(2027, 2, 1),
    )
    before = Meeting.objects.values_list("starts_at", "level", "status", "minutes_version").get(
        pk=theirs.pk
    )
    student = Client()
    mfa_login(student, team.student, PASSWORD)
    m = {"meeting_id": theirs.pk}
    calls = [
        ("get", "detail", m, {}),
        ("get", "edit", m, {}),
        ("post", "edit", m, {"title": "x"}),
        ("get", "duplicate", m, {}),
        ("post", "duplicate", m, {"date": "2027-01-19", "time": "10:00"}),
        ("get", "ics", m, {}),
        ("post", "cancel", m, {}),
        ("post", "level", m, {"level": "PUBLIC"}),
        ("post", "agenda", m, {"agenda": "x"}),
        ("post", "prefill", m, {}),
        ("post", "minutes", m, {"minutes": "x", "version": "1"}),
        ("post", "read", m, {"version": "1"}),
        ("post", "approve", m, {"version": "1"}),
        ("post", "decision", m, {"text": "x"}),
        ("post", "withdraw_decision", {**m, "decision_id": decision.pk}, {}),
        ("post", "action", m, {"title": "x"}),
        ("post", "action_status", {**m, "action_id": action.pk}, {"status": "DONE"}),
        ("post", "document", m, {}),
        ("post", "unlink_document", {**m, "link_id": decision.pk}, {}),
    ]
    tested = {name for _, name, _, _ in calls}
    assert tested | {"list", "new", "register", "objective_actions"} == {
        getattr(p, "name", None) for p in get_resolver("apps.meetings.urls").url_patterns
    }
    for method, name, kw, data in calls:
        response = getattr(student, method)(_url(team.thesis, name, **kw), data)
        assert response.status_code == 403, (name, method)
    after = Meeting.objects.values_list("starts_at", "level", "status", "minutes_version").get(
        pk=theirs.pk
    )
    assert after == before
    assert Decision.objects.get(pk=decision.pk).withdrawn_at is None
    assert ActionItem.objects.get(pk=action.pk).status == "OPEN"
    assert not MinutesAcknowledgement.objects.exists()
    objective = objectives.create_objective(
        thesis=other.thesis,
        user=other.student,
        year=other.thesis.years.get(number=1),
        title="Objectif fictif",
        category="RESEARCH",
        due_date=D(2027, 6, 30),
    )
    url = _url(team.thesis, "objective_actions", objective_id=objective.pk)
    assert student.get(url).status_code == 403


def test_a_sub_object_of_another_meeting_of_the_same_thesis_is_refused(
    team: SimpleNamespace,
) -> None:
    """A decision, action or link id is looked up under the meeting of the URL."""
    mine = _meeting(team)
    other = _meeting(team, starts_at=START - dt.timedelta(days=1))
    decision = services.add_decision(meeting=other, user=team.student, text="Décision fictive")
    action = services.add_action(
        meeting=other,
        user=team.student,
        title="Action fictive",
        assignee=team.student,
        due_date=D(2027, 2, 1),
    )
    client = Client()
    client.force_login(team.student)
    m = {"meeting_id": mine.pk}
    for name, kw, data in (
        ("withdraw_decision", {"decision_id": decision.pk}, {}),
        ("action_status", {"action_id": action.pk}, {"status": "DONE"}),
    ):
        assert client.post(_url(team.thesis, name, **m, **kw), data).status_code == 403
    assert Decision.objects.get(pk=decision.pk).withdrawn_at is None
    assert ActionItem.objects.get(pk=action.pk).status == "OPEN"


def test_a_forged_level_or_document_is_refused(team: SimpleNamespace) -> None:
    """The form offers the readable levels and documents only; a forged value never
    reaches a seal or a link."""
    client = Client()
    client.force_login(team.tutor)
    response = client.post(
        _url(team.thesis, "new"),
        {
            "kind": "SUPERVISION",
            "title": "Titre fictif",
            "date": "2027-01-12",
            "time": "14:00",
            "duration_minutes": "60",
            "location": "VISIO",
            "level": "INTERNAL_LIRMM",  # the tutor cannot read it
            "attendees": [str(team.tutor.pk)],
        },
    )
    assert response.status_code == 400
    assert not Meeting.objects.exists()
    meeting = _meeting(team, attendees=[team.student, team.tutor])
    lirmm = vault.store(
        thesis=team.thesis,
        upload=SimpleUploadedFile("p.pdf", b"%PDF-1.7\n%note fictive\n%%EOF\n"),
        title="Note fictive",
        level=L.INTERNAL_LIRMM,
        user=team.student,
    )
    url = _url(team.thesis, "document", meeting_id=meeting.pk)
    assert client.post(url, {"document": str(lirmm.pk)}).status_code == 400
    assert not MeetingDocument.objects.exists()


# --------------------------------------------------------------------------- rotation


def test_rotation_and_retirement_keep_meeting_texts_readable(
    team: SimpleNamespace, time_machine: Any
) -> None:
    meeting = _meeting(
        team, level=L.CONFIDENTIAL, title="Titre confidentiel fictif",
        attendees=[team.student, team.director],
    )  # fmt: skip
    services.add_decision(meeting=meeting, user=team.student, text="Décision confidentielle")
    call_command("rotate_kek", "CONFIDENTIAL", stdout=StringIO())
    time_machine.move_to(NOW + dt.timedelta(hours=1), tick=True)  # past VAULT_RETIRE_GRACE
    call_command("rotate_kek", "CONFIDENTIAL", "--retire", stdout=StringIO())
    assert set(SealedText.objects.values_list("kek_id", flat=True)) == {"env:CONFIDENTIAL:2"}
    detail = services.meeting_detail(team.director, meeting)
    assert detail.texts["title"] == "Titre confidentiel fictif"
    assert [d.text for d in detail.decisions] == ["Décision confidentielle"]


# --------------------------------------------------------------------------- concurrency


@pytest.mark.serial
def test_two_simultaneous_minutes_saves_keep_one(team: SimpleNamespace) -> None:
    meeting = _meeting(team)
    barrier = threading.Barrier(2, timeout=5)
    outcomes: list[str] = []

    def save(text: str) -> None:
        barrier.wait()
        try:
            services.save_minutes(meeting=meeting, user=team.student, minutes=text, version=0)
            outcomes.append("saved")
        except ValidationError as exc:
            outcomes.append(str(exc.code))

    run_threads(lambda: save("Version A"), lambda: save("Version B"), control=lambda: None)
    assert sorted(outcomes) == ["saved", "stale"]
    assert Meeting.objects.get(pk=meeting.pk).minutes_version == 1
    assert AuditEvent.objects.filter(action="MEETING_MINUTES_UPDATED").count() == 1


@pytest.mark.serial
def test_two_simultaneous_approvals_mark_once(team: SimpleNamespace) -> None:
    meeting = _meeting(team)
    services.save_minutes(meeting=meeting, user=team.student, minutes="CR fictif", version=0)
    barrier = threading.Barrier(2, timeout=5)
    outcomes: list[str] = []

    def approve() -> None:
        barrier.wait()
        try:
            services.acknowledge(
                meeting=meeting, user=team.director, kind="APPROVED", version=1, recent_mfa=True
            )
            outcomes.append("approved")
        except ValidationError as exc:
            outcomes.append(str(exc.code))

    run_threads(approve, approve, control=lambda: None)
    assert sorted(outcomes) == ["already_acknowledged", "approved"]
    assert MinutesAcknowledgement.objects.count() == 1


@pytest.mark.serial
def test_two_simultaneous_reminder_runs_send_once(team: SimpleNamespace, time_machine: Any) -> None:
    _meeting(team)
    mail.outbox.clear()
    barrier = threading.Barrier(2, timeout=5)

    def run() -> None:
        barrier.wait()
        reminders.run(D(2027, 1, 11))

    # The eve's reminder is sent on the eve; rule still_due 5.7.
    time_machine.move_to(dt.datetime(2027, 1, 11, 8, 0, tzinfo=dt.UTC), tick=True)
    run_threads(run, run, control=lambda: None)
    time_machine.move_to(NOW, tick=True)
    assert len(mail.outbox) == 2  # student and director, once each


# --------------------------------------------------------------------------- admin, strangers


def test_no_meeting_model_in_the_django_admin() -> None:
    from django.contrib import admin

    assert {m for m in admin.site._registry if m._meta.app_label == "meetings"} == set()


def test_anonymous_and_strangers_get_nothing(team: SimpleNamespace) -> None:
    meeting = _meeting(team)
    before = AuditEvent.objects.count()
    anonymous = Client()
    for name in ("detail", "ics"):
        response = anonymous.get(_url(team.thesis, name, meeting_id=meeting.pk))
        assert response.status_code == 302 and "/accounts/login/" in response["Location"]
    assert anonymous.get(_url(team.thesis, "list")).status_code == 302
    assert AuditEvent.objects.count() == before  # anonymous traffic never fills the log
    stranger = Client()
    stranger.force_login(UserFactory.create(password=PASSWORD))
    for name in ("detail", "ics"):
        assert stranger.get(_url(team.thesis, name, meeting_id=meeting.pk)).status_code == 403
    assert stranger.get(_url(team.thesis, "list")).status_code == 403
