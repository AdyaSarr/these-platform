"""M9b (Q5): an ST approval awaited without a reference deadline (a journal, a
venue without an edition, a preprint) is reminded to the tutor at « request +
review period », then every week while it is PENDING; through deliver_once
(concerned: an active tutor reading the level; still due: still PENDING on the
same version); category « Échéances »; a link only."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail

from apps.conferences import notifications, reminders, services
from apps.conferences.models import ConferenceNotification
from apps.conferences.tests.helpers import (
    NOW,
    article_version,
    deadline_of,
    edition_of,
    make_team,
    submission_of,
    venue_of,
)

pytestmark = pytest.mark.django_db(transaction=True)
UTC = dt.UTC


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)  # 2027-01-10, 10:00 in Paris


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _at(month: int, day: int, hour: int = 6) -> dt.datetime:
    return dt.datetime(2027, month, day, hour, 0, tzinfo=UTC)


def _waiting() -> list[ConferenceNotification]:
    return list(
        ConferenceNotification.objects.filter(kind="APPROVAL_WAITING").order_by("created_at")
    )


def _journal_request(team: SimpleNamespace) -> Any:
    from apps.conferences.models import Venue

    journal = Venue.objects.filter(thesis=team.thesis, acronym="RFSM").first() or venue_of(
        team, kind="JOURNAL", acronym="RFSM", name="Revue fictive"
    )
    submission = submission_of(team, venue=journal, edition=None,
                               title="TEMOIN-FUITE-TITRE-ATTENTE")  # fmt: skip
    version = article_version(team, submission)
    return services.request_approval(submission=submission, user=team.student, version=version)


def test_at_request_plus_review_period_then_every_week(team: SimpleNamespace) -> None:
    approval = _journal_request(team)  # 2027-01-10 + 30 days: 2027-02-09
    assert reminders.run(_at(2, 8)).waiting == 0
    assert reminders.run(_at(2, 9)).waiting == 1
    assert reminders.run(_at(2, 9, 12)).waiting == 0  # once
    assert reminders.run(_at(2, 15)).waiting == 0  # the same week
    assert reminders.run(_at(2, 16)).waiting == 1  # a week later
    assert reminders.run(_at(2, 23)).waiting == 1  # and the week after
    rows = _waiting()
    assert [r.recipient for r in rows] == [team.tutor] * 3
    assert {r.link_id for r in rows} == {approval.submission_id}
    assert len({r.target_id for r in rows}) == 3


def test_a_missed_day_does_not_skip_a_week(team: SimpleNamespace) -> None:
    _journal_request(team)
    assert reminders.run(_at(2, 11)).waiting == 1  # first run of week 0
    assert reminders.run(_at(2, 20)).waiting == 1  # first run of week 1
    assert reminders.run(_at(2, 23)).waiting == 1  # week 2


def test_the_frozen_review_period_counts(team: SimpleNamespace) -> None:
    services.set_review_days(thesis=team.thesis, user=team.tutor, days=7, recent_mfa=True)
    _journal_request(team)  # 2027-01-10 + 7 days: 2027-01-17
    services.set_review_days(thesis=team.thesis, user=team.tutor, days=60, recent_mfa=True)
    assert reminders.run(_at(1, 16)).waiting == 0
    assert reminders.run(_at(1, 17)).waiting == 1


def test_not_once_decided_or_withdrawn(team: SimpleNamespace) -> None:
    approval = _journal_request(team)
    services.decide_approval(approval=approval, user=team.tutor, decision="GRANTED",
                             recent_mfa=True)  # fmt: skip
    assert reminders.run(_at(2, 9)).waiting == 0
    withdrawn = _journal_request(team)
    services.withdraw_approval(approval=withdrawn, user=team.student)
    assert reminders.run(_at(2, 16)).waiting == 0


def test_an_approval_with_a_reference_deadline_keeps_its_own_reminders(
    team: SimpleNamespace,
) -> None:
    edition = edition_of(team)
    deadline_of(team, edition)  # PAPER, 2027-03-01 AoE
    submission = submission_of(team, edition=edition)
    services.request_approval(submission=submission, user=team.student,
                              version=article_version(team, submission))  # fmt: skip
    for day in (9, 16, 23):
        assert reminders.run(_at(2, day)).waiting == 0


def test_a_preprint_request_has_no_reference_deadline(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    deadline_of(team, edition)
    submission = submission_of(team, edition=edition)
    services.request_approval(submission=submission, user=team.student, purpose="PREPRINT",
                              version=article_version(team, submission))  # fmt: skip
    assert reminders.run(_at(2, 9)).waiting == 1


def test_a_link_only_in_the_category_of_deadlines(team: SimpleNamespace) -> None:
    from apps.activity.routing import CATEGORY_OF, Category

    assert CATEGORY_OF["conferences"]["APPROVAL_WAITING"] == Category.DEADLINE
    approval = _journal_request(team)
    mail.outbox.clear()
    reminders.run(_at(2, 9))
    (message,) = [m for m in mail.outbox if team.tutor.email in m.to]
    assert str(approval.submission_id) in message.body
    assert "TEMOIN-FUITE" not in message.body and "TEMOIN-FUITE" not in message.subject
    assert _waiting()[0].status == "SENT"


def test_still_due_and_concerned_are_checked_again(team: SimpleNamespace) -> None:
    approval = _journal_request(team)
    reminders.run(_at(2, 9))
    (row,) = _waiting()
    assert notifications.concerned(row) and notifications.still_due(row)
    # Another recipient than an active tutor is never concerned.
    row.recipient = team.director
    assert not notifications.concerned(row)
    row.recipient = team.tutor
    services.withdraw_approval(approval=approval, user=team.student)
    assert not notifications.still_due(row)  # no longer PENDING
    assert notifications.skip_reason(row) is not None


def test_a_new_request_on_another_version_is_another_event(team: SimpleNamespace) -> None:
    approval = _journal_request(team)
    reminders.run(_at(2, 9))
    (row,) = _waiting()
    services.withdraw_approval(approval=approval, user=team.student)
    submission = approval.submission
    again = services.request_approval(submission=submission, user=team.student,
                                      version=article_version(team, submission))  # fmt: skip
    assert again.version_id != approval.version_id
    assert not notifications.still_due(row)  # the row named the first request
