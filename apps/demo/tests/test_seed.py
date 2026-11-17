"""demo_seed: a complete, coherent demonstration thesis, written through the
services only, with a verifiable audit chain and no credential generated."""

import datetime as dt
import io
import itertools
import logging
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from allauth.mfa.models import Authenticator
from django.apps import apps
from django.contrib.auth import authenticate
from django.core import mail
from django.core.management import CommandError, call_command
from django.db import models
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Invitation, Membership, Thesis, User
from apps.accounts.permissions import can_access_document
from apps.accounts.roles import Role
from apps.admin_file.models import ItemStatus, RequiredItem
from apps.core.confidentiality import ConfidentialityLevel
from apps.demo import scenario
from apps.objectives import services as objectives
from apps.objectives.models import Objective, Status
from apps.vault.audit import verify_chain
from apps.vault.models import AuditEvent, Document

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.without_vault_keys]

SITE_ORIGIN = "http://testserver"
# Fields that are in the future by design (end of a validity), or a planned
# instant (a meeting's start, typed by a person), not a creation time.
FUTURE_BY_DESIGN = {"expires_at", "active_until", "starts_at", "due_at"}


@pytest.fixture
def demo(settings: Any) -> scenario.SeedResult:
    settings.DEBUG = True
    return scenario.seed()


@pytest.fixture
def run(settings: Any, caplog: pytest.LogCaptureFixture, capsys: Any) -> dict[str, Any]:
    """The command itself, every log record captured from DEBUG up."""
    settings.DEBUG = True
    caplog.set_level(logging.DEBUG)
    out, err = io.StringIO(), io.StringIO()
    call_command("demo_seed", stdout=out, stderr=err)
    captured = capsys.readouterr()
    return {
        "stdout": out.getvalue(),
        "stderr": err.getvalue() + captured.err + captured.out,
        "logs": [f"{r.name} {r.getMessage()} {r.args!r}" for r in caplog.records],
    }


def _thesis() -> Thesis:
    return Thesis.objects.get(title=scenario.DEMO_TITLE)


def _user(role: Role) -> User:
    return Membership.objects.active().get(thesis=_thesis(), role=role).user


# --------------------------------------------------------------------------- journal


def test_every_event_is_tagged_and_the_chain_verifies(demo: scenario.SeedResult) -> None:
    report = verify_chain()
    assert report.events > 50
    assert report.findings == []  # not even INFO: timestamps never go back
    sources = set(AuditEvent.objects.values_list("details_json__source", flat=True))
    assert sources == {"demo_seed"}


def test_audit_verify_passes_with_pinned_key_and_checkpoint(
    demo: scenario.SeedResult, tmp_path: Path
) -> None:
    exported = tmp_path / "checkpoints.txt"
    call_command("audit_checkpoint", "--export", str(exported), stdout=io.StringIO())
    key_out = io.StringIO()
    call_command("audit_checkpoint", "--export-public-key", stdout=key_out)
    key = re.search(r"public key: ([0-9a-f]{64})", key_out.getvalue()).group(1)  # type: ignore[union-attr]
    out = io.StringIO()
    call_command("audit_verify", "--public-key", key, "--checkpoints", str(exported), stdout=out)
    assert "ALTERATION" not in out.getvalue()


def test_every_timestamp_lies_in_its_simulated_step(demo: scenario.SeedResult) -> None:
    """No path took PostgreSQL's clock or the real one during the scenario."""
    steps = demo.steps
    assert [s.start for s in steps] == sorted(s.start for s in steps)
    assert all(a.end < b.start for a, b in itertools.pairwise(steps))
    assert steps[-1].end < timezone.now()
    checked = 0
    for model in apps.get_models():
        fields = [
            f.name
            for f in model._meta.get_fields()
            if isinstance(f, models.DateTimeField) and f.name not in FUTURE_BY_DESIGN
        ]
        if not fields:
            continue
        for row in model._default_manager.values(*fields):
            for name, value in row.items():
                if value is None:
                    continue
                checked += 1
                assert any(s.start <= value <= s.end for s in steps), (
                    f"{model._meta.label}.{name} = {value} outside every step"
                )
    assert checked > 200
    times = list(AuditEvent.objects.order_by("seq").values_list("timestamp", flat=True))
    assert times == sorted(times)


# --------------------------------------------------------------------------- content


def test_thesis_dates_and_team(demo: scenario.SeedResult) -> None:
    thesis = _thesis()
    today = timezone.localdate()
    assert thesis.cifre_start_date < today < thesis.planned_end_date
    year = objectives.current_year(thesis)
    assert year is not None and year.number == 1
    roles = set(Membership.objects.active().filter(thesis=thesis).values_list("role", flat=True))
    assert roles == {
        Role.PHD_STUDENT, Role.DIRECTOR, Role.CO_SUPERVISOR, Role.INDUSTRIAL_TUTOR,
        Role.INDUSTRIAL_CO_SUPERVISOR, Role.CSI_MEMBER,
    }  # fmt: skip
    assert thesis.status == Thesis.Status.SETUP


def test_administrative_file_in_varied_states(demo: scenario.SeedResult) -> None:
    items = {i.code: i for i in RequiredItem.objects.filter(thesis=_thesis())}
    statuses = [i.status for i in items.values()]
    assert statuses.count(ItemStatus.VALIDATED) >= 4
    assert statuses.count(ItemStatus.SUBMITTED) >= 2
    assert statuses.count(ItemStatus.TODO) >= 2
    rejected = [i for i in items.values() if i.status == ItemStatus.REJECTED]
    assert len(rejected) == 1 and rejected[0].reason_comment
    # One of the two validations of the collaboration contract is still awaited.
    assert items["collaboration_contract"].status == ItemStatus.SUBMITTED
    assert items["collaboration_contract"].approvals.count() == 1
    today = timezone.localdate()
    open_items = [i for i in items.values() if i.status != ItemStatus.VALIDATED]
    assert sum(i.due_date is not None and i.due_date >= today for i in open_items) >= 2


def test_documents_at_the_four_levels(demo: scenario.SeedResult) -> None:
    levels = set(
        Document.objects.filter(thesis=_thesis()).values_list("confidentiality", flat=True)
    )
    assert levels == {level.value for level in ConfidentialityLevel}
    assert Document.objects.filter(thesis=_thesis(), current_version=2).exists()


def test_real_permissions_apply(demo: scenario.SeedResult) -> None:
    confidential = Document.objects.filter(
        thesis=_thesis(), confidentiality=ConfidentialityLevel.CONFIDENTIAL
    )
    co_supervisor = _user(Role.CO_SUPERVISOR)
    assert confidential and not any(can_access_document(co_supervisor, d) for d in confidential)


def test_objectives_and_a_real_curve(demo: scenario.SeedResult) -> None:
    thesis = _thesis()
    year = objectives.current_year(thesis)
    assert year is not None
    objs = list(Objective.objects.filter(thesis=thesis))
    assert {o.year_id for o in objs} == {year.pk}
    statuses = {o.status for o in objs}
    assert {Status.ACHIEVED, Status.PARTIAL, Status.IN_PROGRESS, Status.PLANNED} <= statuses
    assert Status.DROPPED in statuses
    assert any(o.level == ConfidentialityLevel.CONFIDENTIAL for o in objs)
    director = _user(Role.DIRECTOR)
    board = objectives.dashboard(director, thesis, year)
    points = [p.value for p in board.stats.monthly if p.value is not None]
    assert len(points) >= 5
    assert points == sorted(points) and len(set(points)) >= 4  # a rising curve
    assert board.overdue == []
    assert board.pending  # a declared result awaits the director


def test_meetings_past_and_upcoming(demo: scenario.SeedResult) -> None:
    from apps.meetings import services as meetings
    from apps.meetings.models import Meeting, MinutesAcknowledgement
    from apps.vault.services import RESTRICTED

    thesis = _thesis()
    student, director = _user(Role.PHD_STUDENT), _user(Role.DIRECTOR)
    now = timezone.now()
    past = Meeting.objects.filter(thesis=thesis, starts_at__lt=now)
    upcoming = Meeting.objects.filter(thesis=thesis, starts_at__gt=now)
    assert past.count() >= 3 and upcoming.count() >= 3
    assert {m.level for m in Meeting.objects.filter(thesis=thesis)} >= {
        "PUBLIC", "INTERNAL_ST", "CONFIDENTIAL", "INTERNAL_LIRMM",
    }  # fmt: skip
    assert all(m.minutes_version >= 1 for m in past)
    assert upcoming.filter(kind=Meeting.Kind.CSI, minutes_version=0).exists()
    kinds = MinutesAcknowledgement.Kind
    assert MinutesAcknowledgement.objects.filter(kind=kinds.APPROVED).exists()
    assert MinutesAcknowledgement.objects.filter(kind=kinds.READ).exists()
    overdue = list(meetings.overdue_actions(thesis))
    assert overdue and overdue[0].objective is not None
    block = meetings.dashboard_block(type("R", (), {"user": student})(), thesis)
    assert block["next_meeting"] is not None and block["overdue_count"] >= 1
    # The tutor's meeting is INTERNAL_ST: the director sees it, its texts restricted.
    tutor_meeting = past.get(kind=Meeting.Kind.ST_TUTOR)
    assert meetings.meeting_detail(director, tutor_meeting).texts["title"] is RESTRICTED


def test_conferences_and_submissions(demo: scenario.SeedResult) -> None:
    from apps.conferences.models import Deadline, StApproval, Submission, Venue

    thesis = _thesis()
    statuses = sorted(Submission.objects.filter(thesis=thesis).values_list("status", flat=True))
    assert statuses == ["ACCEPTED", "REJECTED", "ST_REQUESTED"]
    pending = StApproval.objects.filter(thesis=thesis, decision="PENDING")
    assert pending.count() == 1 and pending.get().request_due is not None
    rejected = Submission.objects.get(thesis=thesis, status="REJECTED")
    decisions = list(rejected.approvals.order_by("created_at").values_list("decision", "reason"))
    assert decisions == [("REFUSED", "CHANGES_REQUESTED"), ("GRANTED", "")]
    zones = set(Deadline.objects.filter(thesis=thesis).values_list("tz_name", flat=True))
    assert {"AoE", "America/Los_Angeles"} <= zones
    real = {"TCHES", "CHES", "CASCADE", "PQCRYPTO", "RWC", "EUROCRYPT", "CRYPTO", "ASIACRYPT",
            "PKC", "ACNS", "DATE", "C2", "COSADE", "CARDIS"}  # fmt: skip
    venues = Venue.objects.filter(thesis=thesis)
    assert venues.count() == 4
    assert not {v.acronym.upper() for v in venues} & real
    assert all(v.url.endswith(".example.org/") for v in venues)


# --------------------------------------------------------------------------- accounts


def test_no_credential_was_generated(demo: scenario.SeedResult) -> None:
    users = list(User.objects.all())
    assert len(users) == 6
    assert not any(u.has_usable_password() for u in users)
    assert not Authenticator.objects.exists()
    for user in users:
        assert authenticate(email=user.email, password="") is None


def test_every_address_is_at_example_org(demo: scenario.SeedResult) -> None:
    from allauth.account.models import EmailAddress

    addresses = [
        *User.objects.values_list("email", flat=True),
        *Invitation.objects.values_list("email", flat=True),
        *EmailAddress.objects.values_list("email", flat=True),
    ]
    assert addresses and all(a.endswith("@example.org") for a in addresses)
    assert {link.email for link in demo.links} == set(User.objects.values_list("email", flat=True))


def test_every_invitation_was_consumed(demo: scenario.SeedResult) -> None:
    assert Invitation.objects.count() == 6
    assert not Invitation.objects.filter(used_at__isnull=True).exists()
    assert mail.outbox == []  # nothing left in the in-memory mailbox


def _csrf_token(response: Any) -> str:
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', response.content.decode())
    assert match, "no CSRF token in the page"
    return match.group(1)


def _set_password_and_sign_in(path: str, email: str) -> Client:
    browser = Client(enforce_csrf_checks=True)
    page = browser.get(path, follow=True)
    assert page.status_code == 200
    form_path = page.redirect_chain[-1][0] if page.redirect_chain else path
    new = secrets.token_urlsafe(16)
    done = browser.post(
        form_path,
        {"password1": new, "password2": new, "csrfmiddlewaretoken": _csrf_token(page)},
        HTTP_ORIGIN=SITE_ORIGIN,
    )
    assert done.status_code == 302, done.content[:300]
    assert "_auth_user_id" not in browser.session  # a reset never signs in
    page = browser.get(reverse("account_login"))
    browser.post(
        reverse("account_login"),
        {"login": email, "password": new, "csrfmiddlewaretoken": _csrf_token(page)},
        HTTP_ORIGIN=SITE_ORIGIN,
    )
    home = browser.get("/")
    assert home.status_code == 302 and home["Location"] == reverse("mfa_index")
    return browser


def test_printed_link_sets_a_password_then_the_second_factor_is_required(
    demo: scenario.SeedResult,
) -> None:
    link = next(x for x in demo.links if x.role == Role.DIRECTOR)
    _set_password_and_sign_in(urlsplit(link.url).path, link.email)
    assert User.objects.get(email=link.email).has_usable_password()


def test_forgotten_password_works_later_for_a_demo_account(
    demo: scenario.SeedResult, django_capture_on_commit_callbacks: Any
) -> None:
    email = next(x.email for x in demo.links if x.role == Role.INDUSTRIAL_TUTOR)
    with dt_later(days=10):  # the demo link has expired meanwhile
        browser = Client(enforce_csrf_checks=True)
        page = browser.get(reverse("account_reset_password"))
        with django_capture_on_commit_callbacks(execute=True):
            browser.post(
                reverse("account_reset_password"),
                {"email": email, "csrfmiddlewaretoken": _csrf_token(page)},
                HTTP_ORIGIN=SITE_ORIGIN,
            )
        assert [m.to for m in mail.outbox] == [[email]]
        link = re.search(r"https?://\S+", str(mail.outbox[0].body)).group(0)  # type: ignore[union-attr]
        _set_password_and_sign_in(urlsplit(link).path, email)


def dt_later(*, days: int) -> Any:
    import time_machine

    return time_machine.travel(timezone.now() + dt.timedelta(days=days), tick=True)


# --------------------------------------------------------------------------- command


def test_command_prints_each_link_once_and_nowhere_else(run: dict[str, Any]) -> None:
    stdout = run["stdout"]
    urls = re.findall(r"https?://\S+", stdout)
    assert len(urls) == 6 and len(set(urls)) == 6
    assert "/invitation/" not in stdout  # invitation tokens never shown
    # /accounts/password/reset/key/<uidb36>-<key>/: the key is the secret part.
    keys = [urlsplit(u).path.rstrip("/").rsplit("/", 1)[-1].split("-", 1)[1] for u in urls]
    haystacks = [run["stderr"], *run["logs"]]
    details = " ".join(str(d) for d in AuditEvent.objects.values_list("details_json", flat=True))
    for key in keys:
        assert len(key) >= 20
        assert all(key not in h for h in haystacks)
        assert key not in details
    for url in urls:
        assert stdout.count(url) == 1


def test_second_run_stops_and_writes_nothing(run: dict[str, Any]) -> None:
    counts = (AuditEvent.objects.count(), User.objects.count(), Document.objects.count())
    with pytest.raises(CommandError, match="already exists"):
        call_command("demo_seed", stdout=io.StringIO())
    assert counts == (AuditEvent.objects.count(), User.objects.count(), Document.objects.count())


def test_a_non_empty_database_is_refused(settings: Any) -> None:
    from apps.vault.audit import AuditAction, audit

    settings.DEBUG = True
    audit(AuditAction.THESIS_CREATED)  # any earlier event
    with pytest.raises(CommandError, match="not empty"):
        call_command("demo_seed", stdout=io.StringIO())
    assert AuditEvent.objects.count() == 1


def test_a_reopened_item_and_a_pending_due_date_proposal(demo: scenario.SeedResult) -> None:
    from apps.admin_file.models import DueDateProposal, ItemNotification, ItemReopening

    items = {i.code: i for i in RequiredItem.objects.filter(thesis=_thesis())}
    reopened = items["employment_contract"]
    assert reopened.status == ItemStatus.TODO
    [row] = ItemReopening.objects.filter(item=reopened)
    assert row.reopened_by == _user(Role.PHD_STUDENT)
    [proposal] = DueDateProposal.objects.filter(item=items["training_agreement"])
    assert proposal.status == DueDateProposal.Status.PENDING
    assert proposal.from_date is not None and proposal.to_date > proposal.from_date
    kinds = set(ItemNotification.objects.values_list("kind", flat=True))
    assert {"REOPENED", "DUE_PROPOSED"} <= kinds
    assert set(ItemNotification.objects.values_list("status", flat=True)) == {"SENT"}
    tutor = _user(Role.INDUSTRIAL_TUTOR)
    assert ItemNotification.objects.filter(kind="REOPENED", recipient=tutor).exists()
