"""M6 security audit: one test (or a pointer to one) per lead of the end-of-module review.

Leads already closed elsewhere:
- 2 time zones: apps/core/tests/test_timezones.py (AoE, Los Angeles across the weeks
  where Paris and the US have not changed yet, nonexistent and ambiguous times,
  Etc/GMT refused); apps/conferences/tests/test_services.py
  ::test_an_aoe_deadline_is_stored_in_utc, ::test_request_due_is_the_paris_date_...
- 3 ST approval on the exact version: test_services.py
  ::test_submitted_needs_the_approved_version_still_current,
  ::test_a_new_final_version_needs_its_own_approval_and_due_date,
  ::test_a_preprint_is_refused_without_approval,
  ::test_resubmitting_elsewhere_never_reuses_the_approval.
- 4 the tutor alone, second factor after the rights: test_services.py
  ::test_only_the_tutor_decides_with_a_recent_second_factor,
  ::test_only_the_tutor_turns_st_work_off, ::test_only_the_tutor_sets_the_period_...;
  test_views.py::test_the_guard_comes_before_the_second_factor, ::test_decide.
- 5 reminders in the deadlines' own zones, once, link only, former members:
  apps/conferences/tests/test_reminders.py.
- 6 strict import, no network: apps/conferences/tests/test_import.py.
- 7 history: apps/conferences/tests/test_models.py
  ::test_a_decided_approval_is_history, ::test_preprints_are_history.
- 8 external links: test_views.py::test_external_links_open_apart_without_referrer,
  ::test_preprint_links_open_apart.
"""

from __future__ import annotations

import ast
import datetime as dt
import logging
import secrets
import threading
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.urls import get_resolver, reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.accounts.tests.helpers import mfa_login
from apps.conferences import reminders, services
from apps.conferences.models import Deadline, Preprint, StApproval, Submission, Venue
from apps.conferences.tests.helpers import (
    NOW,
    D,
    article_version,
    deadline_of,
    edition_of,
    granted,
    submission_of,
)
from apps.vault import storage
from apps.vault.models import AuditEvent, SealedText
from apps.vault.tests.concurrency import run_threads

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = secrets.token_urlsafe(16)
APP = Path(__file__).resolve().parents[1] / "apps" / "conferences"


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


def _url(thesis: Any, name: str, **kw: Any) -> str:
    return reverse(f"conferences:{name}", kwargs={"thesis_id": thesis.pk, **kw})


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


def test_no_marker_in_the_database_the_storage_the_logs_or_the_mails(
    team: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    keys = ("title", "abstract", "external", "notes", "comment", "preprint")
    marks = {k: _marker() for k in keys}
    edition = edition_of(team)
    deadline_of(team, edition)
    student, tutor = Client(), Client()
    mfa_login(student, team.student, PASSWORD)
    mfa_login(tutor, team.tutor, PASSWORD)
    response = student.post(
        _url(team.thesis, "new_submission"),
        {
            "target": f"edition:{edition.pk}",
            "title": marks["title"],
            "abstract": marks["abstract"],
            "external_authors": marks["external"],
            "notes": marks["notes"],
            "objective": "",
            "authors": [str(team.tutor.pk)],
            "level": "CONFIDENTIAL",
        },
    )
    assert response.status_code == 302, response.content[:400]
    submission = Submission.objects.get()
    version = article_version(team, submission)
    s = {"submission_id": submission.pk}
    student.post(
        _url(team.thesis, "request", **s), {"version": str(version.pk), "purpose": "SUBMISSION"}
    )
    approval = StApproval.objects.get()
    tutor.post(
        _url(team.thesis, "decide", **s, approval_id=approval.pk),
        {"decision": "REFUSED", "reason": "CHANGES_REQUESTED", "comment": marks["comment"]},
    )
    student.post(
        _url(team.thesis, "request", **s), {"version": str(version.pk), "purpose": "SUBMISSION"}
    )
    second = StApproval.objects.get(decision="PENDING")
    tutor.post(
        _url(team.thesis, "decide", **s, approval_id=second.pk),
        {"decision": "GRANTED", "reason": "", "comment": ""},
    )
    student.post(
        _url(team.thesis, "preprint", **s),
        {
            "platform": "EPRINT",
            "posted_on": "2027-01-10",
            "url": f"https://eprint.example.org/{marks['preprint']}",
            "version": str(version.pk),
        },
    )
    student.post(
        _url(team.thesis, "status", **s), {"status": "SUBMITTED", "version": str(version.pk)}
    )
    for client in (student, tutor):
        client.get(_url(team.thesis, "submission", **s))
        client.get(_url(team.thesis, "submissions"))
        client.get(reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk}))
    reminders.run(dt.datetime(2027, 2, 28, 6, 0, tzinfo=dt.UTC))
    assert Submission.objects.get().status == "SUBMITTED"
    assert Preprint.objects.count() == 1
    assert StApproval.objects.filter(decision="REFUSED").count() == 1

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
        for line in [*logs, *mails]:
            assert mark not in line
    sealed = SealedText.objects.filter(thesis=team.thesis, slot__gte=0x0301, slot__lte=0x03FF)
    assert sealed.count() == 6 and set(sealed.values_list("level", flat=True)) == {"CONFIDENTIAL"}


# --------------------------------------------------------------------------- IDOR


def test_every_conference_route_refuses_an_object_of_another_thesis(team: SimpleNamespace) -> None:
    other = _team()
    edition = edition_of(other)
    deadline = deadline_of(other, edition)
    theirs = submission_of(other, edition=edition)
    version = article_version(other, theirs)
    approval = services.request_approval(submission=theirs, user=other.student, version=version)
    link = theirs.document_links.get()
    student, tutor = Client(), Client()
    mfa_login(student, team.student, PASSWORD)
    mfa_login(tutor, team.tutor, PASSWORD)
    s = {"submission_id": theirs.pk}
    venue_data = {"kind": "CONFERENCE", "acronym": "X", "name": "x"}
    deadline_data = {"kind": "PAPER", "day": "2027-05-01", "wall": "23:59", "tz_name": "AoE"}
    calls: list[tuple[Client, str, str, dict[str, Any], dict[str, Any]]] = [
        (student, "get", "venue", {"venue_id": edition.venue_id}, {}),
        (student, "post", "venue", {"venue_id": edition.venue_id}, venue_data),
        (student, "post", "new_edition", {"venue_id": edition.venue_id}, {"year": "2028"}),
        (student, "get", "edition", {"edition_id": edition.pk}, {}),
        (student, "post", "edition", {"edition_id": edition.pk}, deadline_data),
        (student, "post", "cancel_deadline", {"deadline_id": deadline.pk}, {}),
        (student, "get", "submission", s, {}),
        (student, "get", "edit_submission", s, {}),
        (student, "post", "edit_submission", s, {"title": "x"}),
        (student, "post", "document", s, {}),
        (student, "post", "unlink_document", {**s, "link_id": link.pk}, {}),
        (student, "post", "request", s, {"version": str(version.pk), "purpose": "SUBMISSION"}),
        (student, "post", "withdraw_approval", {**s, "approval_id": approval.pk}, {}),
        (tutor, "post", "decide", {**s, "approval_id": approval.pk}, {"decision": "GRANTED"}),
        (student, "post", "status", s, {"status": "WITHDRAWN"}),
        (student, "post", "preprint", s, {"platform": "HAL", "posted_on": "2027-01-10", "url": "https://hal.example.org/1"}),
        (student, "post", "level", s, {"level": "PUBLIC"}),
        (tutor, "post", "st_work_off", s, {}),
        (student, "post", "st_work_on", s, {}),
        (student, "post", "resubmit", s, {}),
    ]  # fmt: skip
    tested = {name for _, _, name, _, _ in calls}
    untested = {"deadlines", "policy", "policy_edit", "new_venue", "submissions", "new_submission",
                "objective_submissions"}  # fmt: skip
    assert tested | untested == {
        getattr(p, "name", None) for p in get_resolver("apps.conferences.urls").url_patterns
    }
    for client, method, name, kw, data in calls:
        response = getattr(client, method)(_url(team.thesis, name, **kw), data)
        assert response.status_code == 403, (name, method)
    theirs.refresh_from_db()
    approval.refresh_from_db()
    assert (theirs.status, theirs.level, theirs.st_work) == ("ST_REQUESTED", "CONFIDENTIAL", True)
    assert approval.decision == "PENDING"
    assert Deadline.objects.get(pk=deadline.pk).cancelled_at is None
    assert Venue.objects.filter(thesis=other.thesis).count() == 1
    assert not Preprint.objects.exists()


# --------------------------------------------------------------------------- concurrency


@pytest.mark.serial
def test_two_simultaneous_decisions_decide_once(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    approval = services.request_approval(
        submission=submission, user=team.student, version=article_version(team, submission)
    )
    barrier = threading.Barrier(2, timeout=5)
    outcomes: list[str] = []

    def decide(decision: str) -> None:
        barrier.wait()
        try:
            services.decide_approval(
                approval=approval, user=team.tutor, decision=decision,
                reason="OTHER" if decision == "REFUSED" else "", recent_mfa=True,
            )  # fmt: skip
            outcomes.append(decision)
        except ValidationError as exc:
            outcomes.append(str(exc.code))

    run_threads(lambda: decide("GRANTED"), lambda: decide("REFUSED"), control=lambda: None)
    assert "decided" in outcomes and len(outcomes) == 2
    assert (
        AuditEvent.objects.filter(action__in=["ST_APPROVAL_GRANTED", "ST_APPROVAL_REFUSED"]).count()
        == 1
    )


@pytest.mark.serial
def test_two_simultaneous_requests_leave_one_pending(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    version = article_version(team, submission)
    barrier = threading.Barrier(2, timeout=5)
    outcomes: list[str] = []

    def request() -> None:
        barrier.wait()
        try:
            services.request_approval(submission=submission, user=team.student, version=version)
            outcomes.append("requested")
        except ValidationError as exc:
            outcomes.append(str(exc.code))

    run_threads(request, request, control=lambda: None)
    assert sorted(outcomes) in (["already_pending", "requested"], ["requested", "transition"])
    assert StApproval.objects.filter(decision="PENDING").count() == 1


# --------------------------------------------------------------------------- rotation


def test_rotation_and_retirement_keep_submission_texts_readable(
    team: SimpleNamespace, time_machine: Any
) -> None:
    submission = submission_of(team, title="Titre confidentiel fictif")
    version = article_version(team, submission)
    granted(team, submission, version, purpose="PREPRINT")
    services.add_preprint(
        submission=submission, user=team.student, platform="ARXIV", posted_on=D(2027, 1, 9),
        url="https://arxiv.example.org/fictif", version=version,
    )  # fmt: skip
    call_command("rotate_kek", "CONFIDENTIAL", stdout=StringIO())
    time_machine.move_to(NOW + dt.timedelta(hours=1), tick=True)
    call_command("rotate_kek", "CONFIDENTIAL", "--retire", stdout=StringIO())
    assert set(SealedText.objects.values_list("kek_id", flat=True)) == {"env:CONFIDENTIAL:2"}
    detail = services.submission_detail(team.tutor, submission)
    assert detail.texts["title"] == "Titre confidentiel fictif"
    assert [p.url for p in detail.preprints] == ["https://arxiv.example.org/fictif"]


# --------------------------------------------------------------------------- static


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


FORBIDDEN = {"socket", "http", "http.client", "urllib", "urllib.request", "requests", "httpx",
             "aiohttp", "ftplib", "smtplib.SMTP", "yaml", "pickle", "marshal", "shelve",
             "icalendar"}  # fmt: skip


@pytest.mark.parametrize(
    "path",
    sorted(p for p in APP.rglob("*.py") if "tests" not in p.parts),
    ids=lambda p: str(p.relative_to(APP)),
)
def test_no_network_yaml_or_pickle_in_the_module(path: Path) -> None:
    imported = _imports(path)
    assert not {name for name in imported if name in FORBIDDEN or name.split(".")[0] in FORBIDDEN}


def test_no_conference_model_in_the_django_admin() -> None:
    from django.contrib import admin

    assert {m for m in admin.site._registry if m._meta.app_label == "conferences"} == set()


def test_anonymous_and_strangers_get_nothing(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    before = AuditEvent.objects.count()
    anonymous = Client()
    for name, kw in (("deadlines", {}), ("submission", {"submission_id": submission.pk})):
        response = anonymous.get(_url(team.thesis, name, **kw))
        assert response.status_code == 302 and "/accounts/login/" in response["Location"]
    assert AuditEvent.objects.count() == before
    stranger = Client()
    stranger.force_login(UserFactory.create(password=PASSWORD))
    assert stranger.get(_url(team.thesis, "deadlines")).status_code == 403
    assert (
        stranger.get(_url(team.thesis, "submission", submission_id=submission.pk)).status_code
        == 403
    )
