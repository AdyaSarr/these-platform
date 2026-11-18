"""Services of the conferences (M6): catalogue and time zones, submissions and their
sealed texts, the ST publication approval bound to an exact version (submission,
final version, preprint), resubmission elsewhere, rights and the audit log."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.conferences import services
from apps.conferences.models import (
    ConferenceNotification,
    Preprint,
    StApproval,
    Submission,
)
from apps.conferences.tests.helpers import (
    NOW,
    D,
    L,
    T,
    article_version,
    deadline_of,
    edition_of,
    granted,
    make_team,
    marker,
    submission_of,
    venue_of,
)
from apps.objectives.tests.helpers import objective_of
from apps.vault import services as vault
from apps.vault.audit import AuditAction
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import AuditEvent, SealedText

pytestmark = pytest.mark.django_db(transaction=True)

RESTRICTED = vault.RESTRICTED


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _code(exc: pytest.ExceptionInfo[ValidationError]) -> str | None:
    return exc.value.code


def _details(action: AuditAction) -> list[dict[str, Any]]:
    return [e.details_json for e in AuditEvent.objects.filter(action=action).order_by("seq")]


def _no_text_in_audit(*texts: str) -> None:
    dumped = json.dumps([e.details_json for e in AuditEvent.objects.all()], ensure_ascii=False)
    for text in texts:
        assert text not in dumped


def _status(submission: Submission) -> str:
    return Submission.objects.get(pk=submission.pk).status


# =========================================================================== catalogue


@pytest.mark.parametrize("role", ["student", "director", "co_supervisor", "tutor"])
def test_every_internal_role_maintains_the_catalogue(team: SimpleNamespace, role: str) -> None:
    venue = venue_of(team, user=getattr(team, role))
    assert venue.acronym == "SFCA"


@pytest.mark.parametrize("role", ["csi", "guest", "outsider"])
def test_the_catalogue_is_refused_outside_the_team(team: SimpleNamespace, role: str) -> None:
    with pytest.raises(PermissionDenied):
        venue_of(team, user=getattr(team, role))
    with pytest.raises(PermissionDenied):
        services.upcoming_deadlines(getattr(team, role), team.thesis)


@pytest.mark.parametrize("url", ["http://sfca.example.org/", "javascript:alert(1)", "ftp://x"])
def test_a_venue_url_is_https(team: SimpleNamespace, url: str) -> None:
    with pytest.raises(ValidationError) as exc:
        venue_of(team, url=url)
    assert _code(exc) == "url"


def test_a_journal_has_no_edition(team: SimpleNamespace) -> None:
    journal = venue_of(team, kind="JOURNAL", acronym="RFSM", name="Revue fictive")
    with pytest.raises(ValidationError) as exc:
        edition_of(team, venue=journal)
    assert _code(exc) == "journal"


def test_an_aoe_deadline_is_stored_in_utc(team: SimpleNamespace) -> None:
    deadline = deadline_of(team, edition_of(team))
    assert deadline.due_at == dt.datetime(2027, 3, 2, 11, 59, tzinfo=dt.UTC)
    assert deadline.tz_name == "AoE"
    shown = services.deadline_display(deadline)
    assert shown.paris == (D(2027, 3, 2), T(12, 59))
    assert shown.local == (D(2027, 3, 1), T(23, 59))
    (added,) = _details(AuditAction.DEADLINE_ADDED)
    assert added == {"kind": "PAPER", "due_at": "2027-03-02T11:59:00Z", "tz_name": "AoE"}


def test_a_deadline_time_that_does_not_exist_in_its_zone_is_refused(
    team: SimpleNamespace,
) -> None:
    edition = edition_of(team)
    with pytest.raises(ValidationError) as exc:
        deadline_of(team, edition, day=D(2027, 3, 14), wall=T(2, 30), tz_name="America/Los_Angeles")
    assert _code(exc) == "nonexistent_time"
    with pytest.raises(ValidationError) as exc:
        deadline_of(team, edition, tz_name="Etc/GMT+12")
    assert _code(exc) == "tz_name"


def test_change_and_cancel_a_deadline(team: SimpleNamespace) -> None:
    deadline = deadline_of(team, edition_of(team))
    services.change_deadline(
        deadline=deadline, user=team.director, day=D(2027, 3, 8), wall=T(23, 59), tz_name="AoE"
    )
    deadline.refresh_from_db()
    assert deadline.due_at == dt.datetime(2027, 3, 9, 11, 59, tzinfo=dt.UTC)
    services.cancel_deadline(deadline=deadline, user=team.director)
    deadline.refresh_from_db()
    assert deadline.cancelled_at is not None
    assert services.upcoming_deadlines(team.student, team.thesis) == []
    (changed,) = _details(AuditAction.DEADLINE_CHANGED)
    assert (changed["from"], changed["to"]) == ("2027-03-02T11:59:00Z", "2027-03-09T11:59:00Z")


def test_upcoming_deadlines_are_in_order_and_of_the_thesis(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    late = deadline_of(team, edition, day=D(2027, 4, 1))
    early = deadline_of(team, edition, kind="ABSTRACT", day=D(2027, 2, 20))
    deadline_of(team, edition, kind="NOTIFICATION", day=D(2027, 1, 2))  # past
    other = make_team()
    deadline_of(other, edition_of(other))
    assert [d.pk for d in services.upcoming_deadlines(team.student, team.thesis)] == [
        early.pk,
        late.pk,
    ]


# =========================================================================== submissions


def test_create_seals_every_text_and_audits_codes_only(team: SimpleNamespace) -> None:
    title, abstract, external, notes = marker(), marker(), marker(), marker()
    submission = submission_of(
        team, title=title, abstract=abstract, external_authors=external, notes=notes,
        authors=[team.director],
    )  # fmt: skip
    rows = SealedText.objects.filter(object_id=submission.pk)
    assert rows.count() == 4 and set(rows.values_list("level", flat=True)) == {"CONFIDENTIAL"}
    for row in rows:
        assert title.encode() not in bytes(row.ciphertext)
    (created,) = _details(AuditAction.SUBMISSION_CREATED)
    assert created["level"] == "CONFIDENTIAL" and created["st_work"] is True
    _no_text_in_audit(title, abstract, external, notes)
    detail = services.submission_detail(team.tutor, submission)
    assert detail.texts["title"] == title and detail.texts["external_authors"] == external
    with pytest.raises(PermissionDenied):  # SR-01: not readable, not there
        services.submission_detail(team.co_supervisor, submission)


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor", "csi", "guest"])
def test_only_the_student_writes_submissions(team: SimpleNamespace, role: str) -> None:
    with pytest.raises(PermissionDenied):
        submission_of(team, user=getattr(team, role))


@pytest.mark.parametrize("role", ["csi", "guest", "outsider"])
def test_submissions_are_not_read_outside_the_team(team: SimpleNamespace, role: str) -> None:
    submission = submission_of(team)
    with pytest.raises(PermissionDenied):
        services.submission_detail(getattr(team, role), submission)


def test_st_work_needs_a_level_the_tutor_reads(team: SimpleNamespace) -> None:
    with pytest.raises(ValidationError) as exc:
        submission_of(team, level=L.INTERNAL_LIRMM)
    assert _code(exc) == "st_level"


def test_an_edition_must_be_of_the_venue_and_a_journal_has_none(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    other = venue_of(team, acronym="AFPQ", name="Atelier fictif")
    with pytest.raises(ValidationError) as exc:
        submission_of(team, venue=other, edition=edition)
    assert _code(exc) == "edition"
    journal = venue_of(team, kind="JOURNAL", acronym="RFSM", name="Revue fictive")
    submission_of(team, venue=journal, edition=None)
    with pytest.raises(ValidationError) as exc:
        submission_of(team, venue=other, edition=None)
    assert _code(exc) == "edition"


def test_coauthors_are_internal_members_reading_the_level(team: SimpleNamespace) -> None:
    with pytest.raises(ValidationError) as exc:
        submission_of(team, authors=[team.co_supervisor])  # cannot read CONFIDENTIAL
    assert _code(exc) == "author_level"
    with pytest.raises(ValidationError) as exc:
        submission_of(team, authors=[team.csi])
    assert _code(exc) == "author_not_member"


def test_change_level_reseals_and_needs_a_second_factor(team: SimpleNamespace) -> None:
    submission = submission_of(team, level=L.PUBLIC)
    version = article_version(team, submission)
    approval = services.request_approval(submission=submission, user=team.student, version=version)
    services.decide_approval(
        approval=approval, user=team.tutor, decision="REFUSED", reason="CHANGES_REQUESTED",
        comment="Commentaire fictif du tuteur", recent_mfa=True,
    )  # fmt: skip
    with pytest.raises(ReauthenticationRequiredError):
        services.change_level(
            submission=submission, user=team.student, level=L.INTERNAL_ST, recent_mfa=False
        )
    services.change_level(
        submission=submission, user=team.student, level=L.INTERNAL_ST, recent_mfa=True
    )
    ids = [submission.pk, approval.pk]
    assert set(SealedText.objects.filter(object_id__in=ids).values_list("level", flat=True)) == {
        "INTERNAL_ST"
    }
    with pytest.raises(ValidationError) as exc:
        services.change_level(
            submission=submission, user=team.student, level=L.INTERNAL_LIRMM, recent_mfa=True
        )
    assert _code(exc) == "st_level"


def test_only_the_tutor_turns_st_work_off(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    with pytest.raises(PermissionDenied):
        services.set_st_work(submission=submission, user=team.student, value=False, recent_mfa=True)
    with pytest.raises(ReauthenticationRequiredError):
        services.set_st_work(submission=submission, user=team.tutor, value=False, recent_mfa=False)
    services.set_st_work(submission=submission, user=team.tutor, value=False, recent_mfa=True)
    assert Submission.objects.get(pk=submission.pk).st_work is False
    services.set_st_work(submission=submission, user=team.student, value=True, recent_mfa=False)
    assert Submission.objects.get(pk=submission.pk).st_work is True
    events = _details(AuditAction.SUBMISSION_ST_WORK_CHANGED)
    assert [e["st_work"] for e in events] == [False, True]


# =========================================================================== ST approval


def test_request_due_is_the_paris_date_of_the_paper_deadline_minus_the_period(
    team: SimpleNamespace,
) -> None:
    edition = edition_of(team)
    # 2027-03-01 23:59 AoE = 2027-03-02 12:59 in Paris: the Paris date counts.
    deadline_of(team, edition)
    submission = submission_of(team, edition=edition)
    version = article_version(team, submission)
    approval = services.request_approval(submission=submission, user=team.student, version=version)
    assert (approval.review_days, approval.request_due) == (30, D(2027, 1, 31))
    assert _status(submission) == "ST_REQUESTED"
    notified = ConferenceNotification.objects.filter(kind="APPROVAL_REQUESTED")
    assert {n.recipient for n in notified} == {team.tutor}


def test_the_period_is_frozen_in_the_request(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    deadline_of(team, edition)
    submission = submission_of(team, edition=edition)
    approval = services.request_approval(
        submission=submission, user=team.student, version=article_version(team, submission)
    )
    services.set_review_days(thesis=team.thesis, user=team.tutor, days=45, recent_mfa=True)
    approval.refresh_from_db()
    assert (approval.review_days, approval.request_due) == (30, D(2027, 1, 31))
    assert services.review_days(team.thesis) == 45


def test_only_the_tutor_sets_the_period_and_the_student_is_told(team: SimpleNamespace) -> None:
    with pytest.raises(PermissionDenied):
        services.set_review_days(thesis=team.thesis, user=team.student, days=10, recent_mfa=True)
    with pytest.raises(ReauthenticationRequiredError):
        services.set_review_days(thesis=team.thesis, user=team.tutor, days=10, recent_mfa=False)
    with pytest.raises(ValidationError):
        services.set_review_days(thesis=team.thesis, user=team.tutor, days=181, recent_mfa=True)
    services.set_review_days(thesis=team.thesis, user=team.tutor, days=60, recent_mfa=True)
    (changed,) = _details(AuditAction.PUBLICATION_POLICY_CHANGED)
    assert (changed["from"], changed["to"]) == (30, 60)
    told = ConferenceNotification.objects.filter(kind="POLICY_CHANGED")
    assert {n.recipient for n in told} == {team.student}


def test_a_journal_request_has_no_due_date(team: SimpleNamespace) -> None:
    journal = venue_of(team, kind="JOURNAL", acronym="RFSM", name="Revue fictive")
    submission = submission_of(team, venue=journal, edition=None)
    approval = services.request_approval(
        submission=submission, user=team.student, version=article_version(team, submission)
    )
    assert approval.request_due is None


def test_a_request_needs_st_work_a_linked_version_and_a_tutor(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    other = submission_of(team, title="Autre article fictif")
    foreign = article_version(team, other)
    with pytest.raises(ValidationError) as exc:
        services.request_approval(submission=submission, user=team.student, version=foreign)
    assert _code(exc) == "version"
    version = article_version(team, submission)
    from apps.accounts import services as accounts
    from apps.accounts.models import Membership

    tutor = Membership.objects.active().get(thesis=team.thesis, user=team.tutor)
    accounts.end_membership(tutor, by=team.student)
    with pytest.raises(ValidationError) as exc:
        services.request_approval(submission=submission, user=team.student, version=version)
    assert _code(exc) == "no_tutor"


def test_one_pending_request_per_purpose(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    version = article_version(team, submission)
    services.request_approval(submission=submission, user=team.student, version=version)
    with pytest.raises(ValidationError) as exc:
        services.request_approval(submission=submission, user=team.student, version=version)
    assert _code(exc) in {"already_pending", "transition"}


def test_only_the_tutor_decides_with_a_recent_second_factor(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    approval = services.request_approval(
        submission=submission, user=team.student, version=article_version(team, submission)
    )
    for role in ("student", "director", "co_supervisor"):
        with pytest.raises(PermissionDenied):
            services.decide_approval(
                approval=approval, user=getattr(team, role), decision="GRANTED", recent_mfa=True
            )
    with pytest.raises(ReauthenticationRequiredError):
        services.decide_approval(
            approval=approval, user=team.tutor, decision="GRANTED", recent_mfa=False
        )
    with pytest.raises(ValidationError) as exc:
        services.decide_approval(
            approval=approval, user=team.tutor, decision="REFUSED", recent_mfa=True
        )
    assert _code(exc) == "reason"


def test_a_refusal_keeps_its_reason_and_seals_its_comment(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    approval = services.request_approval(
        submission=submission, user=team.student, version=article_version(team, submission)
    )
    comment = marker()
    services.decide_approval(
        approval=approval, user=team.tutor, decision="REFUSED", reason="PATENT_PENDING",
        comment=comment, recent_mfa=True,
    )  # fmt: skip
    assert _status(submission) == "PREPARING"
    (refused,) = _details(AuditAction.ST_APPROVAL_REFUSED)
    assert refused["reason"] == "PATENT_PENDING"
    _no_text_in_audit(comment)
    detail = services.submission_detail(team.student, submission)
    assert [a.comment for a in detail.approvals] == [comment]
    with pytest.raises(ValidationError) as exc:
        services.decide_approval(
            approval=approval, user=team.tutor, decision="GRANTED", recent_mfa=True
        )
    assert _code(exc) == "decided"
    told = ConferenceNotification.objects.filter(kind="APPROVAL_DECIDED")
    assert {n.recipient for n in told} == {team.student}


# --------------------------------------------------------------------------- submit


def test_submitted_is_refused_without_approval(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    version = article_version(team, submission)
    with pytest.raises(ValidationError) as exc:
        services.set_status(
            submission=submission, user=team.student, status="SUBMITTED", version=version
        )
    assert _code(exc) in {"missing_approval", "transition"}
    assert _status(submission) == "PREPARING"


def test_submitted_needs_the_approved_version_still_current(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    v1 = article_version(team, submission)
    granted(team, submission, v1)
    assert _status(submission) == "ST_GRANTED"
    v2 = article_version(team, submission, document=v1.document)
    with pytest.raises(ValidationError) as exc:
        services.set_status(
            submission=submission, user=team.student, status="SUBMITTED", version=v1
        )
    assert _code(exc) == "approval_outdated"
    with pytest.raises(ValidationError) as exc:
        services.set_status(
            submission=submission, user=team.student, status="SUBMITTED", version=v2
        )
    assert _code(exc) == "missing_approval"
    granted(team, submission, v2)
    services.set_status(submission=submission, user=team.student, status="SUBMITTED", version=v2)
    fresh = Submission.objects.get(pk=submission.pk)
    assert (fresh.status, fresh.submitted_version_id) == ("SUBMITTED", v2.pk)
    assert fresh.submitted_at is not None


def test_without_st_work_a_submission_goes_straight_to_submitted(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    services.set_st_work(submission=submission, user=team.tutor, value=False, recent_mfa=True)
    services.set_status(submission=submission, user=team.student, status="SUBMITTED")
    assert _status(submission) == "SUBMITTED"


def test_status_transitions(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    for status in ("ACCEPTED", "REJECTED", "PUBLISHED", "ST_GRANTED"):
        with pytest.raises(ValidationError) as exc:
            services.set_status(submission=submission, user=team.student, status=status)
        assert _code(exc) == "transition"
    with pytest.raises(PermissionDenied):
        services.set_status(submission=submission, user=team.director, status="WITHDRAWN")


def test_withdrawing_withdraws_the_pending_requests(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    approval = services.request_approval(
        submission=submission, user=team.student, version=article_version(team, submission)
    )
    services.set_status(submission=submission, user=team.student, status="WITHDRAWN")
    approval.refresh_from_db()
    assert approval.decision == "WITHDRAWN"
    with pytest.raises(ValidationError) as exc:
        services.set_status(submission=submission, user=team.student, status="PREPARING")
    assert _code(exc) == "transition"


# --------------------------------------------------------------------------- final version


def _accepted(team: SimpleNamespace) -> tuple[Submission, Any]:
    edition = edition_of(team)
    deadline_of(team, edition)
    deadline_of(team, edition, kind="CAMERA_READY", day=D(2027, 6, 15), tz_name="Europe/Paris")
    submission = submission_of(team, edition=edition)
    v1 = article_version(team, submission)
    granted(team, submission, v1)
    services.set_status(submission=submission, user=team.student, status="SUBMITTED", version=v1)
    services.set_status(submission=submission, user=team.student, status="ACCEPTED")
    return submission, v1


def test_published_with_the_version_already_granted_needs_no_new_request(
    team: SimpleNamespace,
) -> None:
    submission, v1 = _accepted(team)
    services.set_status(submission=submission, user=team.student, status="PUBLISHED", version=v1)
    fresh = Submission.objects.get(pk=submission.pk)
    assert (fresh.status, fresh.published_version_id) == ("PUBLISHED", v1.pk)
    assert StApproval.objects.filter(submission=submission).count() == 1


def test_a_new_final_version_needs_its_own_approval_and_due_date(team: SimpleNamespace) -> None:
    submission, v1 = _accepted(team)
    v2 = article_version(team, submission, document=v1.document)
    with pytest.raises(ValidationError) as exc:
        services.set_status(
            submission=submission, user=team.student, status="PUBLISHED", version=v2
        )
    assert _code(exc) == "missing_approval"
    approval = services.request_approval(
        submission=submission, user=team.student, version=v2, purpose="CAMERA_READY"
    )
    # Camera-ready 2027-06-15 23:59 in Paris, minus 30 days.
    assert approval.request_due == D(2027, 5, 16)
    assert _status(submission) == "ACCEPTED"  # the status does not move for a final version
    services.decide_approval(
        approval=approval, user=team.tutor, decision="GRANTED", recent_mfa=True
    )
    services.set_status(submission=submission, user=team.student, status="PUBLISHED", version=v2)
    assert _status(submission) == "PUBLISHED"


def test_a_camera_ready_request_needs_an_accepted_submission(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    with pytest.raises(ValidationError) as exc:
        services.request_approval(
            submission=submission,
            user=team.student,
            version=article_version(team, submission),
            purpose="CAMERA_READY",
        )
    assert _code(exc) == "transition"


# --------------------------------------------------------------------------- preprint


def test_a_preprint_is_refused_without_approval(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    version = article_version(team, submission)
    with pytest.raises(ValidationError) as exc:
        services.add_preprint(
            submission=submission, user=team.student, platform="EPRINT", posted_on=D(2027, 1, 9),
            url="https://eprint.example.org/2027/001", version=version,
        )  # fmt: skip
    assert _code(exc) == "missing_approval"
    assert not Preprint.objects.exists()


def test_a_preprint_with_approval_seals_its_url(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    version = article_version(team, submission)
    granted(team, submission, version, purpose="PREPRINT")
    assert _status(submission) == "PREPARING"  # a preprint approval does not move the status
    url = "https://eprint.example.org/2027/" + marker()
    preprint = services.add_preprint(
        submission=submission, user=team.student, platform="EPRINT", posted_on=D(2027, 1, 9),
        url=url, version=version,
    )  # fmt: skip
    row = SealedText.objects.get(object_id=preprint.pk)
    assert row.level == "CONFIDENTIAL" and url.encode() not in bytes(row.ciphertext)
    (posted,) = _details(AuditAction.SUBMISSION_PREPRINTED)
    assert posted == {
        "platform": "EPRINT",
        "posted_on": "2027-01-09",
        "version_id": str(version.pk),
    }
    _no_text_in_audit(url)
    assert [p.url for p in services.submission_detail(team.tutor, submission).preprints] == [url]


def test_a_preprint_needs_https_a_past_date_and_a_live_submission(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    services.set_st_work(submission=submission, user=team.tutor, value=False, recent_mfa=True)
    kw: dict[str, Any] = {"submission": submission, "user": team.student, "platform": "ARXIV"}
    with pytest.raises(ValidationError) as exc:
        services.add_preprint(**kw, posted_on=D(2027, 1, 9), url="http://arxiv.example.org/1")
    assert _code(exc) == "url"
    with pytest.raises(ValidationError) as exc:
        services.add_preprint(**kw, posted_on=D(2027, 1, 11), url="https://arxiv.example.org/1")
    assert _code(exc) == "posted_on"
    services.add_preprint(**kw, posted_on=D(2027, 1, 10), url="https://arxiv.example.org/1")
    services.set_status(submission=submission, user=team.student, status="WITHDRAWN")
    with pytest.raises(ValidationError) as exc:
        services.add_preprint(**kw, posted_on=D(2027, 1, 10), url="https://arxiv.example.org/2")
    assert _code(exc) == "terminal"


# --------------------------------------------------------------------------- resubmit


def test_resubmitting_elsewhere_never_reuses_the_approval(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    submission = submission_of(team, edition=edition, authors=[team.tutor])
    v1 = article_version(team, submission)
    granted(team, submission, v1)
    services.set_status(submission=submission, user=team.student, status="SUBMITTED", version=v1)
    services.set_status(submission=submission, user=team.student, status="REJECTED")
    target = edition_of(team, venue=venue_of(team, acronym="AFPQ", name="Atelier fictif"))
    copy = services.resubmit_elsewhere(
        submission=submission, user=team.student, venue=target.venue, edition=target
    )
    assert (copy.status, copy.st_work, copy.resubmission_of_id) == (
        "PREPARING",
        True,
        submission.pk,
    )
    assert {a.user for a in copy.authors.all()} == {team.tutor}
    assert services.submission_detail(team.student, copy).texts["title"] == (
        "Attaque fictive sur un circuit fictif"
    )
    with pytest.raises(ValidationError) as exc:
        services.set_status(submission=copy, user=team.student, status="SUBMITTED", version=v1)
    assert _code(exc) in {"missing_approval", "transition"}
    assert not StApproval.objects.filter(submission=copy).exists()


def test_only_a_rejected_or_withdrawn_submission_is_resubmitted(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    with pytest.raises(ValidationError) as exc:
        services.resubmit_elsewhere(
            submission=submission, user=team.student, venue=submission.venue,
            edition=submission.edition,
        )  # fmt: skip
    assert _code(exc) == "transition"


# =========================================================================== objectives, card


def test_linked_submissions_on_an_objective(team: SimpleNamespace) -> None:
    objective = objective_of(team)
    submission = submission_of(team, objective=objective, level=L.PUBLIC)
    rows = services.submissions_for_objective(team.director, objective)
    assert [(r.submission.pk, r.title) for r in rows] == [
        (submission.pk, "Attaque fictive sur un circuit fictif")
    ]
    with pytest.raises(PermissionDenied):
        services.submissions_for_objective(team.csi, objective)
    other = make_team()
    with pytest.raises(ValidationError) as exc:
        submission_of(team, objective=objective_of(other))
    assert _code(exc) == "objective"


def test_pending_approvals_for_the_tutor(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    approval = services.request_approval(
        submission=submission, user=team.student, version=article_version(team, submission)
    )
    assert [a.pk for a in services.pending_approvals(team.tutor, team.thesis)] == [approval.pk]
    assert services.pending_approvals(team.director, team.thesis) == []


# =========================================================================== end-of-module review


def test_the_target_is_locked_once_an_approval_is_pending_or_granted(team: SimpleNamespace) -> None:
    """Changing the venue of an approved submission would be « submitting elsewhere »
    without a new approval."""
    submission = submission_of(team)
    other = edition_of(team, venue=venue_of(team, acronym="AFPQ", name="Atelier fictif"))
    version = article_version(team, submission)
    approval = services.request_approval(submission=submission, user=team.student, version=version)
    for changes in ({"venue": other.venue, "edition": other}, {"edition": other}):
        with pytest.raises(ValidationError) as exc:
            services.update_submission(submission=submission, user=team.student, changes=changes)
        assert _code(exc) in {"target_locked", "edition"}
    services.decide_approval(
        approval=approval, user=team.tutor, decision="GRANTED", recent_mfa=True
    )
    with pytest.raises(ValidationError) as exc:
        services.update_submission(
            submission=submission,
            user=team.student,
            changes={"venue": other.venue, "edition": other},
        )
    assert _code(exc) == "target_locked"
    # The texts stay editable.
    services.update_submission(submission=submission, user=team.student, changes={"notes": "Note"})
    fresh = submission_of(team, title="Article neuf fictif")
    services.update_submission(
        submission=fresh, user=team.student, changes={"venue": other.venue, "edition": other}
    )
    assert Submission.objects.get(pk=fresh.pk).edition_id == other.pk


def test_a_request_needs_a_document_the_tutor_reads(team: SimpleNamespace) -> None:
    from django.core.files.uploadedfile import SimpleUploadedFile

    submission = submission_of(team)
    lirmm = vault.store(
        thesis=team.thesis,
        upload=SimpleUploadedFile("a.pdf", b"%PDF-1.7\n%article fictif\n%%EOF\n"),
        title="Version LIRMM fictive",
        level=L.INTERNAL_LIRMM,
        user=team.student,
    )
    services.link_document(submission=submission, user=team.student, document=lirmm)
    version = lirmm.versions.get(version=1)
    with pytest.raises(ValidationError) as exc:
        services.request_approval(submission=submission, user=team.student, version=version)
    assert _code(exc) == "tutor_cannot_read"


def test_st_work_off_after_a_grant_lets_the_submission_be_submitted(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    granted(team, submission, article_version(team, submission))
    services.set_st_work(submission=submission, user=team.tutor, value=False, recent_mfa=True)
    services.set_status(submission=submission, user=team.student, status="SUBMITTED")
    assert _status(submission) == "SUBMITTED"


def test_withdraw_a_request(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    approval = services.request_approval(
        submission=submission, user=team.student, version=article_version(team, submission)
    )
    with pytest.raises(PermissionDenied):
        services.withdraw_approval(approval=approval, user=team.tutor)
    services.withdraw_approval(approval=approval, user=team.student)
    approval.refresh_from_db()
    assert approval.decision == "WITHDRAWN" and approval.decided_by is None
    assert _status(submission) == "PREPARING"
    with pytest.raises(ValidationError) as exc:
        services.withdraw_approval(approval=approval, user=team.student)
    assert _code(exc) == "decided"


def test_unlink_a_document(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    article_version(team, submission)
    link = submission.document_links.get()
    with pytest.raises(PermissionDenied):
        services.unlink_document(link=link, user=team.director)
    services.unlink_document(link=link, user=team.student)
    assert not submission.document_links.exists()
    (unlinked,) = _details(AuditAction.SUBMISSION_DOCUMENT_UNLINKED)
    assert unlinked == {"document_id": str(link.document_id)}
