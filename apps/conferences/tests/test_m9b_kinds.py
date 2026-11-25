"""M9b in M6: the venue kinds added (seminar, PhD students' day, other: an
OPTIONAL edition) and the form of a communication (``presentation_kind``: paper,
poster, talk, invited talk), with exactly the same ST approval circuit
(docs/modules/M09-portfolio.md, M9b; docs/decisions.md, 2026-11-25)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command

from apps.conferences import reminders, services
from apps.conferences.models import PresentationKind, Submission, Venue, VenueKind
from apps.conferences.tests.helpers import (
    NOW,
    D,
    L,
    article_version,
    deadline_of,
    edition_of,
    granted,
    make_team,
    submission_of,
    venue_of,
)
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)

NEW_KINDS = ("SEMINAR", "PHD_DAY", "OTHER")
OPTIONAL = ("SEMINAR", "PHD_DAY", "OTHER")


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _venue(team: SimpleNamespace, kind: str) -> Venue:
    return venue_of(team, kind=kind, acronym=f"V-{kind}", name=f"Lieu fictif {kind}")


# --------------------------------------------------------------------------- the kinds


def test_the_kinds() -> None:
    assert set(VenueKind.values) == {"CONFERENCE", "WORKSHOP", "JOURNAL", *NEW_KINDS}
    assert "POSTER" not in VenueKind.values  # a poster is a form, not a venue (Q6)
    assert PresentationKind.values == ["PAPER", "POSTER", "TALK", "INVITED_TALK"]


@pytest.mark.parametrize("kind", OPTIONAL)
def test_a_new_kind_takes_an_edition_or_none(team: SimpleNamespace, kind: str) -> None:
    venue = _venue(team, kind)
    assert venue.has_editions and not venue.needs_edition
    without = submission_of(team, venue=venue, edition=None)
    assert without.edition_id is None
    edition = edition_of(team, venue=venue)
    with_edition = submission_of(team, edition=edition)
    assert with_edition.edition_id == edition.pk


def test_a_conference_still_needs_an_edition_and_a_journal_has_none(
    team: SimpleNamespace,
) -> None:
    conference = venue_of(team)
    assert conference.needs_edition and conference.has_editions
    with pytest.raises(ValidationError) as exc:
        submission_of(team, venue=conference, edition=None)
    assert exc.value.code == "edition"
    journal = venue_of(team, kind="JOURNAL", acronym="RFSM", name="Revue fictive")
    assert not journal.has_editions and not journal.needs_edition
    with pytest.raises(ValidationError) as exc:
        edition_of(team, venue=journal)
    assert exc.value.code == "journal"


def test_an_edition_of_another_venue_is_refused(team: SimpleNamespace) -> None:
    seminar = _venue(team, "SEMINAR")
    other = edition_of(team, venue=_venue(team, "PHD_DAY"))
    with pytest.raises(ValidationError) as exc:
        submission_of(team, venue=seminar, edition=other)
    assert exc.value.code == "edition"


# --------------------------------------------------------------------------- the form


def test_the_form_of_a_communication(team: SimpleNamespace) -> None:
    paper = submission_of(team)
    assert paper.presentation_kind == "PAPER"
    poster = submission_of(team, presentation_kind="POSTER", title="Poster fictif")
    assert poster.presentation_kind == "POSTER"
    event = AuditEvent.objects.filter(action="SUBMISSION_CREATED").order_by("seq").last()
    assert event is not None and event.details_json["presentation_kind"] == "POSTER"
    with pytest.raises(ValidationError) as exc:
        submission_of(team, presentation_kind="KEYNOTE")
    assert exc.value.code == "presentation_kind"
    changed = services.update_submission(
        submission=poster, user=team.student, changes={"presentation_kind": "TALK"}
    )
    assert changed == ["presentation_kind"]
    assert Submission.objects.get(pk=poster.pk).presentation_kind == "TALK"


def test_the_form_can_change_once_the_target_is_locked(team: SimpleNamespace) -> None:
    """The form of a communication is not its target: ST's approval is on a version."""
    edition = edition_of(team)
    deadline_of(team, edition)
    submission = submission_of(team, edition=edition)
    granted(team, submission, article_version(team, submission))
    services.update_submission(
        submission=submission, user=team.student, changes={"presentation_kind": "POSTER"}
    )
    assert Submission.objects.get(pk=submission.pk).presentation_kind == "POSTER"


def test_a_resubmission_keeps_the_form(team: SimpleNamespace) -> None:
    poster = submission_of(team, presentation_kind="POSTER", st_work=False, level=L.PUBLIC)
    services.set_status(submission=poster, user=team.student, status="WITHDRAWN")
    copy = services.resubmit_elsewhere(
        submission=poster, user=team.student, venue=_venue(team, "SEMINAR"), edition=None
    )
    assert copy.presentation_kind == "POSTER"


# --------------------------------------------------------------------------- the ST circuit


@pytest.mark.parametrize("kind", NEW_KINDS)
@pytest.mark.parametrize("form", ["POSTER", "TALK", "INVITED_TALK"])
def test_the_same_st_approval_circuit(team: SimpleNamespace, kind: str, form: str) -> None:
    submission = submission_of(team, venue=_venue(team, kind), edition=None,
                               presentation_kind=form)  # fmt: skip
    assert submission.st_work is True  # the default, whatever the kind and the form
    version = article_version(team, submission)
    with pytest.raises(ValidationError) as exc:
        services.set_status(submission=submission, user=team.student, status="SUBMITTED",
                            version=version)  # fmt: skip
    assert exc.value.code == "missing_approval"
    approval = services.request_approval(submission=submission, user=team.student,
                                         version=version)  # fmt: skip
    assert approval.request_due is None  # no edition: no reference deadline
    services.decide_approval(approval=approval, user=team.tutor, decision="GRANTED",
                             recent_mfa=True)  # fmt: skip
    services.set_status(submission=submission, user=team.student, status="SUBMITTED",
                        version=version)  # fmt: skip
    services.set_status(submission=submission, user=team.student, status="ACCEPTED")
    final = article_version(team, submission)
    with pytest.raises(ValidationError) as exc:
        services.set_status(submission=submission, user=team.student, status="PUBLISHED",
                            version=final)  # fmt: skip
    assert exc.value.code == "missing_approval"


def test_only_the_tutor_turns_st_work_off_for_a_communication(team: SimpleNamespace) -> None:
    talk = submission_of(team, venue=_venue(team, "SEMINAR"), edition=None,
                         presentation_kind="TALK")  # fmt: skip
    with pytest.raises(PermissionDenied):
        services.set_st_work(submission=talk, user=team.student, value=False, recent_mfa=True)
    off = services.set_st_work(submission=talk, user=team.tutor, value=False, recent_mfa=True)
    assert off.st_work is False
    assert AuditEvent.objects.filter(action="SUBMISSION_ST_WORK_CHANGED").count() == 1


# --------------------------------------------------------------------------- deadlines


def test_a_seminar_without_edition_has_no_deadline_reminder(team: SimpleNamespace) -> None:
    talk = submission_of(team, venue=_venue(team, "SEMINAR"), edition=None,
                         presentation_kind="TALK")  # fmt: skip
    assert services.request_due_dates(team.thesis) == []
    result = reminders.run(NOW.replace(month=2, day=20))
    assert (result.deadlines, result.request_dues) == (0, 0)
    assert talk.edition_id is None


def test_a_phd_day_with_an_edition_is_reminded_like_a_conference(team: SimpleNamespace) -> None:
    from apps.conferences.models import ConferenceNotification

    edition = edition_of(team, venue=_venue(team, "PHD_DAY"))
    deadline_of(team, edition, kind="ABSTRACT", day=D(2027, 3, 1))
    deadline_of(team, edition, kind="PAPER", day=D(2027, 3, 1))
    submission = submission_of(team, edition=edition, presentation_kind="POSTER")
    due = services.request_due_dates(team.thesis)
    assert [(s.pk, d) for s, d in due] == [(submission.pk, D(2027, 1, 31))]
    reminders.run(NOW.replace(month=2, day=22, hour=12))  # J-7 in AoE
    kinds = set(ConferenceNotification.objects.values_list("kind", "window"))
    assert ("DEADLINE", "J7") in kinds


def test_the_ical_feed_holds_the_deadlines_of_an_edition_only(team: SimpleNamespace) -> None:
    from apps.feeds.tests.helpers import fetch, new_feed

    seminar = submission_of(team, venue=_venue(team, "SEMINAR"), edition=None,
                            presentation_kind="TALK")  # fmt: skip
    day = edition_of(team, venue=_venue(team, "PHD_DAY"))
    deadline = deadline_of(team, day, day=D(2027, 2, 1))
    submission_of(team, edition=day, presentation_kind="POSTER")
    _feed, token = new_feed(team.student, team.thesis)
    text = fetch(token).content.decode()
    assert f"deadline-{deadline.pk}@" in text
    assert str(seminar.pk) not in text and "Lieu fictif" not in text


# --------------------------------------------------------------------------- the JSON import


def test_the_import_takes_the_new_kinds(team: SimpleNamespace, tmp_path: Path) -> None:
    catalogue = {
        "format": 1,
        "venues": [
            {"kind": "SEMINAR", "acronym": "SEMF", "name": "Séminaire fictif"},
            {"kind": "PHD_DAY", "acronym": "JDF", "name": "Journée fictive des doctorants",
             "editions": [{"year": 2027}]},
            {"kind": "OTHER", "acronym": "AUTF", "name": "Autre lieu fictif"},
        ],
    }  # fmt: skip
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps(catalogue), encoding="utf-8")
    call_command("conferences_import", str(path), "--thesis", str(team.thesis.pk),
                 "--as", team.student.email, "--apply")  # fmt: skip
    kinds = set(Venue.objects.filter(thesis=team.thesis).values_list("kind", flat=True))
    assert kinds == {"SEMINAR", "PHD_DAY", "OTHER"}


def test_the_import_still_refuses_an_edition_of_a_journal(
    team: SimpleNamespace, tmp_path: Path
) -> None:
    from django.core.management.base import CommandError

    catalogue = {"format": 1, "venues": [
        {"kind": "JOURNAL", "acronym": "RFSM", "name": "Revue fictive",
         "editions": [{"year": 2027}]},
    ]}  # fmt: skip
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps(catalogue), encoding="utf-8")
    with pytest.raises(CommandError):
        call_command("conferences_import", str(path), "--thesis", str(team.thesis.pk),
                     "--as", team.student.email, "--apply")  # fmt: skip
    assert not Venue.objects.filter(thesis=team.thesis).exists()
