"""Database constraints of the conferences (M6): every CHECK and UNIQUE is hit in SQL,
bypassing the services; decided approvals and preprints are history."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.db import IntegrityError, models, transaction

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory
from apps.conferences.models import (
    ConferenceNotification,
    Deadline,
    Edition,
    HistoryError,
    Preprint,
    PublicationPolicy,
    StApproval,
    Submission,
    SubmissionAuthor,
    SubmissionDocument,
    Venue,
)
from apps.vault import formats
from apps.vault.models import Document, DocumentVersion

pytestmark = pytest.mark.django_db

NOW = dt.datetime(2027, 1, 10, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def team() -> SimpleNamespace:
    thesis = ThesisFactory.create()
    student = MembershipFactory.create(thesis=thesis, role=Role.PHD_STUDENT).user
    tutor = MembershipFactory.create(thesis=thesis, role=Role.INDUSTRIAL_TUTOR).user
    return SimpleNamespace(thesis=thesis, student=student, tutor=tutor)


def _refused(create: Any) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        create()


def _venue(team: SimpleNamespace, **kw: Any) -> Venue:
    fields: dict[str, Any] = {
        "thesis": team.thesis,
        "kind": "CONFERENCE",
        "acronym": "SFCA",
        "name": "Symposium fictif",
    }
    fields.update(kw)
    return Venue.objects.create(**fields)


def _submission(team: SimpleNamespace, **kw: Any) -> Submission:
    fields: dict[str, Any] = {"thesis": team.thesis, "venue": _venue(team, acronym=f"V{id(kw)}")}
    fields.update(kw)
    return Submission.objects.create(**fields)


def _version(team: SimpleNamespace) -> DocumentVersion:
    document = Document.objects.create(
        thesis=team.thesis, title="Article fictif", confidentiality="PUBLIC", owner=team.student
    )
    return DocumentVersion.objects.create(
        document=document,
        version=1,
        ciphertext_path="x" * 40,
        wrapped_dek=b"\x00" * 60,
        kek_id="env:PUBLIC:1",
        size=1,
        mime_type="application/pdf",
    )


# --------------------------------------------------------------------------- catalogue


def test_venues(team: SimpleNamespace) -> None:
    _venue(team, url="https://example.org/sfca")
    _refused(lambda: _venue(team))  # same thesis, kind, acronym
    _venue(team, kind="WORKSHOP")
    _refused(lambda: _venue(team, acronym="X1", url="http://example.org"))
    _refused(lambda: _venue(team, acronym="X2", kind="BOGUS"))
    _refused(lambda: _venue(team, acronym=""))


def test_editions(team: SimpleNamespace) -> None:
    venue = _venue(team)
    Edition.objects.create(thesis=team.thesis, venue=venue, year=2027)
    _refused(lambda: Edition.objects.create(thesis=team.thesis, venue=venue, year=2027))
    _refused(lambda: Edition.objects.create(thesis=team.thesis, venue=venue, year=1999))
    _refused(
        lambda: Edition.objects.create(
            thesis=team.thesis,
            venue=venue,
            year=2028,
            event_start=dt.date(2028, 5, 3),
            event_end=dt.date(2028, 5, 2),
        )
    )


def test_deadlines(team: SimpleNamespace) -> None:
    edition = Edition.objects.create(thesis=team.thesis, venue=_venue(team), year=2027)

    def deadline(**kw: Any) -> Deadline:
        fields: dict[str, Any] = {
            "thesis": team.thesis,
            "edition": edition,
            "kind": "PAPER",
            "due_at": NOW,
            "tz_name": "AoE",
        }
        fields.update(kw)
        return Deadline.objects.create(**fields)

    deadline()
    deadline()  # two sessions of one kind are allowed
    _refused(lambda: deadline(kind="BOGUS"))
    _refused(lambda: deadline(tz_name=""))


@pytest.mark.parametrize(("days", "ok"), [(0, True), (180, True), (181, False)])
def test_review_days_bounds(team: SimpleNamespace, days: int, ok: bool) -> None:
    if ok:
        PublicationPolicy.objects.create(thesis=team.thesis, st_review_days=days)
    else:
        _refused(lambda: PublicationPolicy.objects.create(thesis=team.thesis, st_review_days=days))


def test_one_policy_per_thesis(team: SimpleNamespace) -> None:
    PublicationPolicy.objects.create(thesis=team.thesis)
    _refused(lambda: PublicationPolicy.objects.create(thesis=team.thesis))


# --------------------------------------------------------------------------- submissions


def test_submission_defaults_and_levels(team: SimpleNamespace) -> None:
    submission = _submission(team)
    assert (submission.status, submission.st_work, submission.level) == (
        "PREPARING",
        True,
        "PUBLIC",
    )
    _refused(lambda: _submission(team, level="INTERNAL_LIRMM"))  # the tutor could not read it
    _submission(team, level="INTERNAL_LIRMM", st_work=False)
    _refused(lambda: _submission(team, level="BOGUS", st_work=False))
    _refused(lambda: _submission(team, status="BOGUS"))


def test_submission_dates_follow_the_status(team: SimpleNamespace) -> None:
    for status in ("SUBMITTED", "ACCEPTED", "REJECTED", "PUBLISHED"):
        _refused(lambda status=status: _submission(team, status=status))
    _refused(lambda: _submission(team, status="ACCEPTED", submitted_at=NOW, published_at=NOW))
    _refused(lambda: _submission(team, status="PUBLISHED", submitted_at=NOW))
    _submission(team, status="PUBLISHED", submitted_at=NOW, published_at=NOW)
    _submission(team, status="WITHDRAWN")


def test_authors_and_documents_once(team: SimpleNamespace) -> None:
    submission = _submission(team)
    SubmissionAuthor.objects.create(submission=submission, user=team.student)
    _refused(lambda: SubmissionAuthor.objects.create(submission=submission, user=team.student))
    document = _version(team).document
    SubmissionDocument.objects.create(submission=submission, document=document)
    _refused(lambda: SubmissionDocument.objects.create(submission=submission, document=document))


# --------------------------------------------------------------------------- approvals


def _approval(team: SimpleNamespace, submission: Submission, **kw: Any) -> StApproval:
    fields: dict[str, Any] = {
        "submission": submission,
        "purpose": "SUBMISSION",
        "version": kw.pop("version", None) or _version(team),
        "review_days": 30,
        "requested_by": team.student,
    }
    fields.update(kw)
    return StApproval.objects.create(**fields)


def test_one_pending_approval_per_purpose(team: SimpleNamespace) -> None:
    submission = _submission(team)
    version = _version(team)
    _approval(team, submission, version=version)
    _refused(lambda: _approval(team, submission, version=version))
    _approval(team, submission, version=version, purpose="PREPRINT")


def test_a_decision_is_complete(team: SimpleNamespace) -> None:
    submission = _submission(team)
    version = _version(team)
    a = {"version": version}
    _refused(lambda: _approval(team, submission, decision="GRANTED", **a))
    _refused(lambda: _approval(team, submission, decision="GRANTED", decided_at=NOW, **a))
    _refused(  # the requester never decides
        lambda: _approval(
            team, submission, decision="GRANTED", decided_at=NOW, decided_by=team.student, **a
        )
    )
    _refused(  # a refusal has a reason
        lambda: _approval(
            team, submission, decision="REFUSED", decided_at=NOW, decided_by=team.tutor, **a
        )
    )
    _refused(  # a grant has none
        lambda: _approval(
            team,
            submission,
            decision="GRANTED",
            decided_at=NOW,
            decided_by=team.tutor,
            reason="OTHER",
            **a,
        )
    )
    _refused(lambda: _approval(team, submission, purpose="BOGUS", **a))
    _refused(lambda: _approval(team, submission, review_days=181, **a))
    _approval(
        team, submission, decision="REFUSED", decided_at=NOW, decided_by=team.tutor,
        reason="PATENT_PENDING", **a,
    )  # fmt: skip
    _approval(team, submission, decision="WITHDRAWN", decided_at=NOW, **a)


def test_a_decided_approval_is_history(team: SimpleNamespace) -> None:
    submission = _submission(team)
    pending = _approval(team, submission)
    pending.decision, pending.decided_at, pending.decided_by = "GRANTED", NOW, team.tutor
    pending.save()  # PENDING -> decided: allowed once
    pending.decision = "REFUSED"
    with pytest.raises(HistoryError):
        pending.save()
    with pytest.raises(HistoryError):
        pending.delete()
    with pytest.raises(HistoryError):
        StApproval.objects.filter(pk=pending.pk).update(decision="PENDING")
    with pytest.raises(HistoryError):
        StApproval.objects.filter(pk=pending.pk).delete()


def test_preprints_are_history(team: SimpleNamespace) -> None:
    submission = _submission(team)
    preprint = Preprint.objects.create(
        submission=submission, platform="EPRINT", posted_on=dt.date(2027, 1, 9)
    )
    _refused(
        lambda: Preprint.objects.create(
            submission=submission, platform="BOGUS", posted_on=dt.date(2027, 1, 9)
        )
    )
    with pytest.raises(HistoryError):
        preprint.save()
    with pytest.raises(HistoryError):
        preprint.delete()


def test_one_notification_per_target_window_and_recipient(team: SimpleNamespace) -> None:
    import uuid

    target = uuid.uuid4()
    fields = {"thesis": team.thesis, "kind": "DEADLINE", "target_id": target, "window": "J7"}
    ConferenceNotification.objects.create(recipient=team.student, **fields)
    _refused(lambda: ConferenceNotification.objects.create(recipient=team.student, **fields))
    ConferenceNotification.objects.create(recipient=team.tutor, **fields)


# --------------------------------------------------------------------------- slots


CONFERENCE_SLOTS = {
    "conferences.Submission.title": 0x0301,
    "conferences.Submission.abstract": 0x0302,
    "conferences.Submission.external_authors": 0x0303,
    "conferences.Submission.notes": 0x0304,
    "conferences.StApproval.comment": 0x0305,
    "conferences.Preprint.url": 0x0306,
}


def test_sealed_slots_of_the_conferences() -> None:
    assert {k: v for k, v in formats.TEXT_SLOTS.items() if k.startswith("conferences.")} == (
        CONFERENCE_SLOTS
    )


def test_no_free_text_column_on_submissions() -> None:
    for model in (Submission, SubmissionAuthor, StApproval, Preprint):
        for field in model._meta.concrete_fields:
            if isinstance(field, models.CharField | models.TextField):
                assert field.choices, (model.__name__, field.name)
