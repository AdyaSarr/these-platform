"""Publication details (M9b): one row per submission of M6, the ADUM category and
the date presented in clear, DOI, HAL id, volume, pages and BibTeX key sealed at
the SUBMISSION's level (never a level of their own); read through
Submission.objects.readable_by and sees(SUBMISSION_READ); written by the student
alone, also after the defence (statuses.py: ``portfolio_publications``)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection

from apps.accounts.models import Thesis
from apps.activity.models import Event
from apps.conferences.models import Submission
from apps.portfolio import services
from apps.portfolio.models import PublicationDetails
from apps.portfolio.tests import helpers as h
from apps.vault.models import AuditEvent, SealedText

pytestmark = pytest.mark.django_db(transaction=True)
L = h.L


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _values(**kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"adum_category": "JOURNAL_ARTICLE", "presented_on": None, "doi": "",
                           "hal_id": "", "volume": "", "pages": "", "bibtex_key": ""}  # fmt: skip
    out.update(kw)
    return out


# --------------------------------------------------------------------------- the model


def test_no_level_of_their_own() -> None:
    names = {f.name for f in PublicationDetails._meta.concrete_fields}
    assert "level" not in names
    assert {"thesis", "submission", "adum_category", "presented_on"} <= names


@pytest.mark.parametrize(
    ("venue", "form", "category"),
    [("JOURNAL", "PAPER", "JOURNAL_ARTICLE"), ("CONFERENCE", "PAPER", "CONFERENCE_PAPER"),
     ("WORKSHOP", "PAPER", "CONFERENCE_PAPER"), ("CONFERENCE", "POSTER", "POSTER"),
     ("CONFERENCE", "TALK", "ORAL_COMMUNICATION"), ("JOURNAL", "INVITED_TALK", "INVITED_TALK"),
     ("SEMINAR", "TALK", "SEMINAR"), ("SEMINAR", "PAPER", "SEMINAR"),
     ("SEMINAR", "INVITED_TALK", "INVITED_TALK"), ("PHD_DAY", "POSTER", "POSTER"),
     ("PHD_DAY", "TALK", "ORAL_COMMUNICATION"), ("PHD_DAY", "PAPER", "ORAL_COMMUNICATION"),
     ("OTHER", "PAPER", "OTHER"), ("OTHER", "TALK", "ORAL_COMMUNICATION")],
)  # fmt: skip
def test_the_suggested_category(venue: str, form: str, category: str) -> None:
    assert services.suggested_category(venue, form) == category


def test_publications_and_communications() -> None:
    from apps.portfolio.models import PUBLICATION_CATEGORIES, AdumCategory

    assert {"JOURNAL_ARTICLE", "CONFERENCE_PAPER"} == PUBLICATION_CATEGORIES
    assert set(AdumCategory.values) == {"JOURNAL_ARTICLE", "CONFERENCE_PAPER",
                                        "ORAL_COMMUNICATION", "POSTER", "INVITED_TALK",
                                        "SEMINAR", "OTHER"}  # fmt: skip


# --------------------------------------------------------------------------- writing


def test_the_student_writes_and_reads_back(team: SimpleNamespace) -> None:
    submission = h.publication(team)
    details = services.set_publication_details(
        submission=submission, user=team.student,
        values=_values(doi="10.12345/fictif.2027", hal_id="hal-01234567v2", volume="12",
                       pages="101-117", bibtex_key="fictif:2027",
                       presented_on=h.D(2027, 1, 14)),
    )  # fmt: skip
    assert details.thesis_id == team.thesis.pk and details.submission_id == submission.pk
    row = services.publication_detail(team.student, submission)
    assert row.texts == {
        "doi": "10.12345/fictif.2027",
        "hal_id": "hal-01234567v2",
        "volume": "12",
        "pages": "101-117",
        "bibtex_key": "fictif:2027",
    }
    assert row.category == "JOURNAL_ARTICLE" and row.details.presented_on == h.D(2027, 1, 14)
    event = AuditEvent.objects.filter(action="PUBLICATION_DETAILS_SET").get()
    assert event.details_json["fields"] == sorted(
        ["adum_category", "presented_on", "doi", "hal_id", "volume", "pages", "bibtex_key"]
    )
    # Unchanged values: nothing written, nothing journaled; a cleared text is erased.
    services.set_publication_details(submission=submission, user=team.student,
                                     values=_values(doi="10.12345/fictif.2027",
                                                    hal_id="hal-01234567v2", volume="12",
                                                    pages="101-117", bibtex_key="fictif:2027",
                                                    presented_on=h.D(2027, 1, 14)))  # fmt: skip
    assert AuditEvent.objects.filter(action="PUBLICATION_DETAILS_SET").count() == 1
    services.set_publication_details(submission=submission, user=team.student,
                                     values=_values(presented_on=h.D(2027, 1, 14)))  # fmt: skip
    assert services.publication_detail(team.student, submission).texts["doi"] == ""
    assert not SealedText.objects.filter(object_id=details.pk).exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [("doi", "11.1234/x"), ("doi", "10.12/x"), ("doi", "10.12345/a b"), ("doi", "10.1234/x\x00"),
     ("hal_id", "HAL-01234567"), ("hal_id", "hal-1234"), ("pages", "12 à 15"),
     ("pages", "{12}"), ("bibtex_key", "clé"), ("bibtex_key", "a,b"), ("bibtex_key", "x" * 65),
     ("volume", "1\n2"), ("volume", "x" * 33), ("adum_category", "PATENT")],
)  # fmt: skip
def test_strict_validation(team: SimpleNamespace, field: str, value: str) -> None:
    submission = h.publication(team)
    with pytest.raises(ValidationError) as exc:
        services.set_publication_details(submission=submission, user=team.student,
                                         values=_values(**{field: value}))  # fmt: skip
    assert exc.value.code == field
    assert not PublicationDetails.objects.exists()


def test_the_date_presented_is_inside_the_thesis(team: SimpleNamespace) -> None:
    submission = h.publication(team)
    with pytest.raises(ValidationError) as exc:
        services.set_publication_details(submission=submission, user=team.student,
                                         values=_values(presented_on=h.D(2040, 1, 1)))  # fmt: skip
    assert exc.value.code == "presented_on"


@pytest.mark.parametrize("attr", h.NOT_STUDENT)
def test_only_the_student_writes(team: SimpleNamespace, attr: str) -> None:
    submission = h.publication(team)
    with pytest.raises(PermissionDenied):
        services.set_publication_details(submission=submission, user=getattr(team, attr),
                                         values=_values(doi="10.12345/x"))  # fmt: skip
    assert not PublicationDetails.objects.exists()


def test_a_submission_of_another_thesis_is_refused(team: SimpleNamespace) -> None:
    other = h.make_team()
    theirs = h.publication(other)
    with pytest.raises(PermissionDenied):
        services.publication_for(team.student, team.thesis, theirs.pk, operation="detail")


def test_allowed_after_the_defence_frozen_once_closed(team: SimpleNamespace) -> None:
    submission = h.publication(team)
    Thesis.objects.filter(pk=team.thesis.pk).update(status="DEFENDED", defended_on=h.D(2027, 1, 15))
    h.details(team, submission, doi="10.12345/apres.soutenance")
    Thesis.objects.filter(pk=team.thesis.pk).update(status="CLOSED")
    with pytest.raises((PermissionDenied, ValidationError)):
        h.details(team, submission, doi="10.12345/apres.cloture")
    row = services.publication_detail(team.director, submission)
    assert row.texts["doi"] == "10.12345/apres.soutenance"


# --------------------------------------------------------------------------- reading


@pytest.mark.parametrize("attr", h.SUBMISSION_READERS)
def test_the_readers_of_the_submission_read_its_details(team: SimpleNamespace, attr: str) -> None:
    submission = h.publication(team, L.PUBLIC)
    h.details(team, submission, doi="10.12345/public")
    user = getattr(team, attr)
    assert services.publication_for(user, team.thesis, submission.pk, operation="d") == submission
    assert services.publication_detail(user, submission).texts["doi"] == "10.12345/public"
    rows = services.publication_list(user, team.thesis)
    assert [r.submission.pk for r in rows] == [submission.pk]


@pytest.mark.parametrize("attr", ["csi", "guest", "outsider"])
def test_the_csi_member_and_the_guest_read_nothing(team: SimpleNamespace, attr: str) -> None:
    submission = h.publication(team, L.PUBLIC)
    h.details(team, submission, doi="10.12345/public")
    with pytest.raises(PermissionDenied):
        services.publication_for(getattr(team, attr), team.thesis, submission.pk, operation="d")
    with pytest.raises(PermissionDenied):
        services.publication_list(getattr(team, attr), team.thesis)


def test_an_unreadable_submission_answers_as_an_unknown_one(team: SimpleNamespace) -> None:
    hidden = h.publication(team, L.INTERNAL_LIRMM)
    h.details(team, hidden, doi="10.12345/interne")
    with pytest.raises(PermissionDenied):
        services.publication_for(team.tutor, team.thesis, hidden.pk, operation="detail")
    denied = AuditEvent.objects.filter(action="PORTFOLIO_ACCESS_DENIED").order_by("seq").last()
    assert denied is not None and denied.details_json["reason"] == "unreadable_level"
    assert services.publication_list(team.tutor, team.thesis) == []
    seen = PublicationDetails.objects.readable_by(team.tutor, team.thesis)
    assert not seen.exists()
    assert PublicationDetails.objects.readable_by(team.director, team.thesis).count() == 1


def test_the_existence_follows_the_submission_level_after_a_change(team: SimpleNamespace) -> None:
    from apps.conferences import services as conferences

    submission = h.publication(team, L.CONFIDENTIAL)
    h.details(team, submission, doi="10.12345/confidentiel")
    assert PublicationDetails.objects.readable_by(team.tutor, team.thesis).count() == 1
    conferences.change_level(submission=submission, user=team.student, level=L.INTERNAL_LIRMM,
                             recent_mfa=True)  # fmt: skip
    assert not PublicationDetails.objects.readable_by(team.tutor, team.thesis).exists()
    fresh = Submission.objects.get(pk=submission.pk)
    assert services.publication_detail(team.co_supervisor, fresh).texts["doi"] == (
        "10.12345/confidentiel"
    )


def test_no_text_in_clear_in_the_database(team: SimpleNamespace) -> None:
    submission = h.publication(team)
    h.details(team, submission, volume="TEMOIN-FUITE-VOLUME", bibtex_key="TEMOIN-FUITE-CLE")
    with connection.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        for (table,) in cursor.fetchall():
            cursor.execute(f'SELECT CAST(t AS text) FROM "{table}" t')  # noqa: S608  # nosec B608
            dump = "\n".join(row[0] for row in cursor.fetchall())
            assert "TEMOIN-FUITE" not in dump, table


def test_no_event_of_the_activity_feed(team: SimpleNamespace) -> None:
    submission = h.publication(team)
    before = Event.objects.count()
    h.details(team, submission, doi="10.12345/x")
    assert Event.objects.count() == before
