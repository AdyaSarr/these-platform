"""Services of the meetings (M5a): rights, levels, sealed texts, minutes and their
acknowledgements, decisions, actions, prefilled agenda, duplication, documents,
notification rows and the audit log (codes only, never a text)."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.tests.factories import ThesisFactory
from apps.meetings import services
from apps.meetings.models import (
    ActionItem,
    Meeting,
    MeetingNotification,
    MinutesAcknowledgement,
)
from apps.meetings.tests.helpers import START, L, make_team, marker, meeting_of
from apps.objectives.tests.helpers import approved, objective_of
from apps.vault import services as vault
from apps.vault.audit import AuditAction
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import AuditEvent, SealedText

pytestmark = pytest.mark.django_db(transaction=True)

RESTRICTED = vault.RESTRICTED
Ack = MinutesAcknowledgement.Kind


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


def _store(team: SimpleNamespace, level: L = L.PUBLIC, thesis: Any = None) -> Any:
    upload = SimpleUploadedFile("note.pdf", b"%PDF-1.4\n%fictif\n", content_type="application/pdf")
    owner = team.student
    return vault.store(
        thesis=thesis or team.thesis, upload=upload, title="Note fictive", level=level, user=owner
    )


# =========================================================================== create


def test_create_seals_every_text_and_audits_codes_only(team: SimpleNamespace) -> None:
    title, detail, link = marker(), marker(), "https://visio.example.org/" + marker()
    meeting = meeting_of(team, title=title, location_detail=detail, visio_link=link)
    assert SealedText.objects.filter(object_id=meeting.pk).count() == 3
    for row in SealedText.objects.filter(object_id=meeting.pk):
        assert title.encode() not in bytes(row.ciphertext)
        assert row.level == "PUBLIC"
    (created,) = _details(AuditAction.MEETING_CREATED)
    assert created["kind"] == "SUPERVISION"
    assert created["level"] == "PUBLIC"
    assert created["attendees"] == 3
    _no_text_in_audit(title, detail, link)
    detail_view = services.meeting_detail(team.director, meeting)
    assert detail_view.texts["title"] == title
    assert detail_view.texts["visio_link"] == link


@pytest.mark.parametrize("role", ["csi", "guest", "outsider"])
def test_only_internal_members_create(team: SimpleNamespace, role: str) -> None:
    with pytest.raises(PermissionDenied):
        meeting_of(team, user=getattr(team, role), attendees=[team.student])
    assert not Meeting.objects.exists()
    (denied,) = _details(AuditAction.MEETING_ACCESS_DENIED)
    assert denied["operation"] == "create"


@pytest.mark.parametrize("role", ["student", "director", "co_supervisor", "tutor"])
def test_every_internal_role_creates(team: SimpleNamespace, role: str) -> None:
    user = getattr(team, role)
    meeting = meeting_of(team, user=user, attendees=[team.student, user])
    assert meeting.created_by == user


@pytest.mark.parametrize("role", ["csi", "guest", "outsider"])
def test_attendees_are_internal_members(team: SimpleNamespace, role: str) -> None:
    with pytest.raises(ValidationError) as exc:
        meeting_of(team, attendees=[team.student, getattr(team, role)])
    assert _code(exc) == "attendee_not_member"


def test_an_attendee_of_another_thesis_is_refused(team: SimpleNamespace) -> None:
    other = make_team()
    with pytest.raises(ValidationError) as exc:
        meeting_of(team, attendees=[team.student, other.director])
    assert _code(exc) == "attendee_not_member"


@pytest.mark.parametrize(
    ("level", "role"),
    [
        (L.INTERNAL_ST, "director"),
        (L.INTERNAL_ST, "co_supervisor"),
        (L.INTERNAL_LIRMM, "tutor"),
        (L.CONFIDENTIAL, "co_supervisor"),
    ],
)
def test_every_attendee_reads_the_level(team: SimpleNamespace, level: L, role: str) -> None:
    with pytest.raises(ValidationError) as exc:
        meeting_of(team, level=level, attendees=[team.student, getattr(team, role)])
    assert _code(exc) == "attendee_level"


def test_meeting_dates_are_bounded(team: SimpleNamespace) -> None:
    # make_team(): CIFRE start 2026-12-01, planned end 2029-11-30.
    early = dt.datetime(2026, 8, 31, 12, 0, tzinfo=dt.UTC)
    late = dt.datetime(2030, 12, 1, 12, 0, tzinfo=dt.UTC)
    for starts_at in (early, late):
        with pytest.raises(ValidationError) as exc:
            meeting_of(team, starts_at=starts_at)
        assert _code(exc) == "out_of_bounds"
    meeting_of(team, starts_at=dt.datetime(2026, 9, 1, 8, 0, tzinfo=dt.UTC))
    # 2030-11-30 23:30 in Paris is still the last allowed day.
    meeting_of(team, starts_at=dt.datetime(2030, 11, 30, 22, 30, tzinfo=dt.UTC))


def test_presentation_needs_its_venue(team: SimpleNamespace) -> None:
    with pytest.raises(ValidationError) as exc:
        meeting_of(team, kind=Meeting.Kind.PRESENTATION)
    assert _code(exc) == "presentation_venue"
    meeting_of(team, kind=Meeting.Kind.PRESENTATION, presentation_venue=Meeting.Venue.TEAM_SEMINAR)


def test_invitations_go_to_the_attendees_but_the_author(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    rows = MeetingNotification.objects.filter(meeting=meeting, kind="INVITED")
    assert {r.recipient for r in rows} == {team.director, team.co_supervisor}


# =========================================================================== reading


def test_a_non_attendee_supervisor_sees_the_meeting_and_texts_by_level(
    team: SimpleNamespace,
) -> None:
    title = marker()
    meeting = meeting_of(
        team, level=L.INTERNAL_ST, title=title, attendees=[team.student, team.tutor]
    )
    seen = services.meeting_detail(team.co_supervisor, meeting)
    assert seen.texts["title"] is RESTRICTED
    assert seen.meeting.starts_at == START
    assert services.meeting_detail(team.tutor, meeting).texts["title"] == title
    rows = services.meeting_list(team.director, team.thesis, past=False)
    assert [(r.meeting.pk, r.title) for r in rows] == [(meeting.pk, RESTRICTED)]


@pytest.mark.parametrize("role", ["csi", "guest", "outsider"])
def test_reading_is_refused_outside_the_internal_team(team: SimpleNamespace, role: str) -> None:
    meeting = meeting_of(team)
    with pytest.raises(PermissionDenied):
        services.meeting_detail(getattr(team, role), meeting)
    with pytest.raises(PermissionDenied):
        services.meeting_for(getattr(team, role), team.thesis, meeting.pk, operation="detail")


def test_meeting_for_refuses_another_thesis(team: SimpleNamespace) -> None:
    other = make_team()
    meeting = meeting_of(other, title=marker())
    with pytest.raises(PermissionDenied):
        services.meeting_for(team.student, team.thesis, meeting.pk, operation="detail")
    with pytest.raises(PermissionDenied):
        services.meeting_for(team.student, team.thesis, "not-a-uuid", operation="detail")


def test_one_audited_read_per_detail_of_a_confidential_meeting(team: SimpleNamespace) -> None:
    meeting = meeting_of(
        team, level=L.CONFIDENTIAL, attendees=[team.student, team.director, team.tutor]
    )
    services.save_agenda(meeting=meeting, user=team.student, agenda="Ordre du jour fictif")
    services.add_decision(meeting=meeting, user=team.student, text="Décision fictive")
    before = AuditEvent.objects.filter(action=AuditAction.SEALED_TEXT_READ).count()
    services.meeting_detail(team.director, meeting)
    after = AuditEvent.objects.filter(action=AuditAction.SEALED_TEXT_READ).count()
    assert after == before + 1


# =========================================================================== writing


def test_a_supervisor_neither_author_nor_attendee_cannot_edit(team: SimpleNamespace) -> None:
    meeting = meeting_of(team, attendees=[team.student, team.director])
    with pytest.raises(PermissionDenied):
        services.save_agenda(meeting=meeting, user=team.tutor, agenda="Ajout fictif")
    (denied,) = _details(AuditAction.MEETING_ACCESS_DENIED)
    assert denied["reason"] == "not_attendee"
    services.save_agenda(meeting=meeting, user=team.director, agenda="Ajout fictif")


def test_update_date_notifies_and_bumps_the_ics_sequence(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    later = START + dt.timedelta(days=7)
    services.update_meeting(meeting=meeting, user=team.director, changes={"starts_at": later})
    meeting.refresh_from_db()
    assert (meeting.starts_at, meeting.ics_sequence) == (later, 1)
    changed = MeetingNotification.objects.filter(meeting=meeting, kind="CHANGED")
    assert {r.recipient for r in changed} == {team.student, team.co_supervisor}
    (updated,) = _details(AuditAction.MEETING_UPDATED)
    assert updated["fields"] == ["starts_at"]


def test_a_title_change_alone_notifies_nobody(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    title = marker()
    services.update_meeting(meeting=meeting, user=team.student, changes={"title": title})
    meeting.refresh_from_db()
    assert meeting.ics_sequence == 0
    assert not MeetingNotification.objects.filter(kind="CHANGED").exists()
    assert services.meeting_detail(team.student, meeting).texts["title"] == title
    _no_text_in_audit(title)


def test_cancel(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    services.cancel_meeting(meeting=meeting, user=team.student)
    meeting.refresh_from_db()
    assert meeting.is_cancelled and meeting.ics_sequence == 1
    cancelled = MeetingNotification.objects.filter(meeting=meeting, kind="CANCELLED")
    assert {r.recipient for r in cancelled} == {team.director, team.co_supervisor}
    with pytest.raises(ValidationError) as exc:
        services.save_agenda(meeting=meeting, user=team.student, agenda="Trop tard")
    assert _code(exc) == "cancelled"
    with pytest.raises(ValidationError) as exc:
        services.cancel_meeting(meeting=meeting, user=team.student)
    assert _code(exc) == "cancelled"


def test_set_attendees_invites_the_new_ones_only(team: SimpleNamespace) -> None:
    meeting = meeting_of(team, attendees=[team.student, team.director])
    MeetingNotification.objects.all().delete()
    services.set_attendees(
        meeting=meeting, user=team.student, attendees=[team.student, team.director, team.tutor]
    )
    invited = MeetingNotification.objects.filter(meeting=meeting, kind="INVITED")
    assert {r.recipient for r in invited} == {team.tutor}
    assert {a.user for a in meeting.attendees.all()} == {team.student, team.director, team.tutor}


# =========================================================================== level


def test_change_level_needs_a_recent_second_factor(team: SimpleNamespace) -> None:
    meeting = meeting_of(team, attendees=[team.student, team.director])
    with pytest.raises(ReauthenticationRequiredError):
        services.change_level(
            meeting=meeting, user=team.student, level=L.CONFIDENTIAL, recent_mfa=False
        )


def test_change_level_reseals_every_text(team: SimpleNamespace) -> None:
    meeting = meeting_of(team, attendees=[team.student, team.director])
    services.save_agenda(meeting=meeting, user=team.student, agenda="Ordre du jour fictif")
    services.add_decision(meeting=meeting, user=team.student, text="Décision fictive")
    services.add_action(
        meeting=meeting,
        user=team.student,
        title="Action fictive",
        assignee=team.student,
        due_date=dt.date(2027, 2, 1),
    )
    services.change_level(meeting=meeting, user=team.student, level=L.CONFIDENTIAL, recent_mfa=True)
    meeting.refresh_from_db()
    assert meeting.level == "CONFIDENTIAL"
    ids = [meeting.pk, *meeting.decisions.values_list("pk", flat=True)]
    ids += list(meeting.actions.values_list("pk", flat=True))
    rows = SealedText.objects.filter(object_id__in=ids)
    assert rows.count() == 4
    assert set(rows.values_list("level", flat=True)) == {"CONFIDENTIAL"}
    assert services.meeting_detail(team.co_supervisor, meeting).texts["title"] is RESTRICTED


def test_change_level_excluding_an_attendee_is_refused(team: SimpleNamespace) -> None:
    meeting = meeting_of(team, attendees=[team.student, team.co_supervisor])
    with pytest.raises(ValidationError) as exc:
        services.change_level(
            meeting=meeting, user=team.student, level=L.CONFIDENTIAL, recent_mfa=True
        )
    assert _code(exc) == "attendee_level"


# =========================================================================== minutes


def _minutes(team: SimpleNamespace, meeting: Meeting, text: str = "CR fictif", v: int = 0) -> None:
    services.save_minutes(meeting=meeting, user=team.student, minutes=text, version=v)


def test_minutes_versions_and_stale_edits(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    _minutes(team, meeting)
    meeting.refresh_from_db()
    assert meeting.minutes_version == 1
    assert meeting.minutes_updated_by == team.student
    with pytest.raises(ValidationError) as exc:
        _minutes(team, meeting, "Autre version", v=0)
    assert _code(exc) == "stale"
    _minutes(team, meeting, "Seconde version", v=1)
    meeting.refresh_from_db()
    assert meeting.minutes_version == 2
    assert services.meeting_detail(team.director, meeting).texts["minutes"] == "Seconde version"


def test_first_minutes_notify_every_attendee_later_ones_those_who_marked(
    team: SimpleNamespace,
) -> None:
    meeting = meeting_of(team)
    _minutes(team, meeting)
    first = MeetingNotification.objects.filter(meeting=meeting, kind="MINUTES")
    assert {r.recipient for r in first} == {team.director, team.co_supervisor}
    services.acknowledge(meeting=meeting, user=team.director, kind=Ack.READ, version=1)
    _minutes(team, meeting, "Seconde version", v=1)
    assert {r.recipient for r in MeetingNotification.objects.filter(kind="MINUTES")} == {
        team.director,
        team.co_supervisor,
    }
    assert MeetingNotification.objects.filter(kind="MINUTES", recipient=team.director).count() == 2


def test_csi_minutes_are_not_available_in_m5a(team: SimpleNamespace) -> None:
    meeting = meeting_of(team, kind=Meeting.Kind.CSI)
    with pytest.raises(ValidationError) as exc:
        _minutes(team, meeting)
    assert _code(exc) == "csi_pending"
    with pytest.raises(ValidationError) as exc:
        services.add_decision(meeting=meeting, user=team.student, text="Décision fictive")
    assert _code(exc) == "csi_pending"
    with pytest.raises(ValidationError) as exc:
        services.add_action(
            meeting=meeting,
            user=team.student,
            title="Action fictive",
            assignee=team.student,
            due_date=dt.date(2027, 2, 1),
        )
    assert _code(exc) == "csi_pending"
    services.save_agenda(meeting=meeting, user=team.student, agenda="Ordre du jour fictif")


def test_a_meeting_with_minutes_cannot_become_a_csi(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    _minutes(team, meeting)
    with pytest.raises(ValidationError) as exc:
        services.update_meeting(
            meeting=meeting, user=team.student, changes={"kind": Meeting.Kind.CSI}
        )
    assert _code(exc) == "csi_pending"


def test_acknowledgements(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    with pytest.raises(ValidationError) as exc:
        services.acknowledge(meeting=meeting, user=team.director, kind=Ack.READ, version=1)
    assert _code(exc) == "no_minutes"
    _minutes(team, meeting)
    with pytest.raises(PermissionDenied):
        services.acknowledge(meeting=meeting, user=team.tutor, kind=Ack.READ, version=1)
    with pytest.raises(ReauthenticationRequiredError):
        services.acknowledge(meeting=meeting, user=team.director, kind=Ack.APPROVED, version=1)
    services.acknowledge(meeting=meeting, user=team.co_supervisor, kind=Ack.READ, version=1)
    services.acknowledge(
        meeting=meeting, user=team.director, kind=Ack.APPROVED, version=1, recent_mfa=True
    )
    with pytest.raises(ValidationError) as exc:
        services.acknowledge(
            meeting=meeting, user=team.director, kind=Ack.APPROVED, version=1, recent_mfa=True
        )
    assert _code(exc) == "already_acknowledged"
    detail = services.meeting_detail(team.student, meeting)
    assert detail.marks == {team.director.pk: "APPROVED", team.co_supervisor.pk: "READ"}
    _minutes(team, meeting, "Seconde version", v=1)
    with pytest.raises(ValidationError) as exc:
        services.acknowledge(meeting=meeting, user=team.director, kind=Ack.READ, version=1)
    assert _code(exc) == "stale"
    detail = services.meeting_detail(team.student, meeting)
    assert detail.marks == {}
    assert detail.older_marks == {team.director.pk: "APPROVED", team.co_supervisor.pk: "READ"}
    events = _details(AuditAction.MEETING_MINUTES_ACKNOWLEDGED)
    assert [(e["kind"], e["version"]) for e in events] == [("READ", 1), ("APPROVED", 1)]


# =========================================================================== decisions, actions


def test_decisions_are_withdrawn_never_deleted(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    text = marker()
    decision = services.add_decision(meeting=meeting, user=team.director, text=text)
    services.withdraw_decision(decision=decision, user=team.student)
    decision.refresh_from_db()
    assert decision.withdrawn_at is not None
    register = services.decision_register(team.tutor, team.thesis, withdrawn=True)
    assert [(r.decision.pk, r.text) for r in register] == [(decision.pk, text)]
    assert services.decision_register(team.tutor, team.thesis) == []
    _no_text_in_audit(text)


def test_decision_register_filters(team: SimpleNamespace) -> None:
    first = meeting_of(team)
    second = meeting_of(
        team,
        kind=Meeting.Kind.ST_TUTOR,
        starts_at=START + dt.timedelta(days=40),
        attendees=[team.student, team.tutor],
    )
    a = services.add_decision(meeting=first, user=team.student, text="Décision A")
    b = services.add_decision(meeting=second, user=team.student, text="Décision B")
    by_kind = services.decision_register(team.director, team.thesis, kind="ST_TUTOR")
    assert [r.decision.pk for r in by_kind] == [b.pk]
    by_dates = services.decision_register(
        team.director, team.thesis, start=dt.date(2027, 1, 1), end=dt.date(2027, 1, 31)
    )
    assert [r.decision.pk for r in by_dates] == [a.pk]
    with pytest.raises(PermissionDenied):
        services.decision_register(team.csi, team.thesis)


def test_actions(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    action = services.add_action(
        meeting=meeting,
        user=team.student,
        title="Relire le chapitre fictif",
        assignee=team.tutor,  # any internal member, attendee or not
        due_date=dt.date(2027, 2, 1),
    )
    services.set_action_status(action=action, user=team.tutor, status=ActionItem.Status.DONE)
    action.refresh_from_db()
    assert action.status == "DONE" and action.done_at is not None
    services.set_action_status(action=action, user=team.student, status=ActionItem.Status.OPEN)
    action.refresh_from_db()
    assert action.done_at is None
    with pytest.raises(ValidationError) as exc:
        services.add_action(
            meeting=meeting,
            user=team.student,
            title="Action fictive",
            assignee=team.csi,
            due_date=dt.date(2027, 2, 1),
        )
    assert _code(exc) == "assignee"


def test_an_action_status_by_neither_assignee_nor_writer_is_refused(team: SimpleNamespace) -> None:
    meeting = meeting_of(team, attendees=[team.student, team.director])
    action = services.add_action(
        meeting=meeting,
        user=team.student,
        title="Action fictive",
        assignee=team.student,
        due_date=dt.date(2027, 2, 1),
    )
    with pytest.raises(PermissionDenied):
        services.set_action_status(action=action, user=team.tutor, status=ActionItem.Status.DONE)


def test_an_action_links_an_objective_of_the_same_thesis(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    objective = objective_of(team)
    action = services.add_action(
        meeting=meeting,
        user=team.student,
        title="Action liée fictive",
        assignee=team.student,
        due_date=dt.date(2027, 2, 1),
        objective=objective,
    )
    linked = services.actions_for_objective(team.director, objective)
    assert [(r.action.pk, r.title) for r in linked] == [(action.pk, "Action liée fictive")]
    other = make_team()
    with pytest.raises(ValidationError) as exc:
        services.add_action(
            meeting=meeting,
            user=team.student,
            title="Action fictive",
            assignee=team.student,
            due_date=dt.date(2027, 2, 1),
            objective=objective_of(other),
        )
    assert _code(exc) == "objective"
    with pytest.raises(PermissionDenied):
        services.actions_for_objective(team.csi, objective)


def test_overdue_is_decided_on_the_date_in_paris(team: SimpleNamespace, time_machine: Any) -> None:
    meeting = meeting_of(team)
    action = services.add_action(
        meeting=meeting,
        user=team.student,
        title="Action fictive",
        assignee=team.student,
        due_date=dt.date(2027, 1, 12),
    )
    time_machine.move_to(dt.datetime(2027, 1, 12, 22, 30, tzinfo=dt.UTC))  # 23:30 Paris
    assert list(services.overdue_actions(team.thesis)) == []
    time_machine.move_to(dt.datetime(2027, 1, 12, 23, 30, tzinfo=dt.UTC))  # 00:30 Paris, 13th
    assert list(services.overdue_actions(team.thesis)) == [action]


# =========================================================================== prefill


def _action_in(team: SimpleNamespace, level: L, title: str, attendees: list[Any]) -> None:
    source = meeting_of(team, level=level, attendees=attendees, title="Source fictive")
    services.add_action(
        meeting=source,
        user=team.student,
        title=title,
        assignee=team.student,
        due_date=dt.date(2027, 3, 1),
    )


def test_prefill_copies_only_items_read_by_every_role_of_the_meeting(
    team: SimpleNamespace, time_machine: Any
) -> None:
    public, lirmm, st, conf = marker(), marker(), marker(), marker()
    _action_in(team, L.PUBLIC, public, [team.student])
    _action_in(team, L.INTERNAL_LIRMM, lirmm, [team.student])
    _action_in(team, L.INTERNAL_ST, st, [team.student])
    _action_in(team, L.CONFIDENTIAL, conf, [team.student])
    late_public = marker()
    late_conf = marker()
    approved(team, title=late_public, due_date=dt.date(2027, 1, 5))
    approved(team, title=late_conf, due_date=dt.date(2027, 1, 5), level=L.CONFIDENTIAL)
    time_machine.move_to(dt.datetime(2027, 1, 10, 9, 0, tzinfo=dt.UTC))

    target = meeting_of(team, level=L.CONFIDENTIAL, attendees=[team.student, team.director])
    count = services.prefill_agenda(meeting=target, user=team.student)
    agenda = services.meeting_detail(team.student, target).texts["agenda"]
    # CONFIDENTIAL is read by student, director, tutor: PUBLIC and CONFIDENTIAL items only.
    assert public in agenda and conf in agenda and late_public in agenda and late_conf in agenda
    assert lirmm not in agenda and st not in agenda
    assert count == 4
    assert SealedText.objects.get(object_id=target.pk, slot=0x0202).level == "CONFIDENTIAL"

    public_target = meeting_of(team, attendees=[team.student])
    services.save_agenda(meeting=public_target, user=team.student, agenda="Point déjà prévu")
    services.prefill_agenda(meeting=public_target, user=team.student)
    agenda = services.meeting_detail(team.student, public_target).texts["agenda"]
    assert agenda.startswith("Point déjà prévu")
    assert public in agenda and late_public in agenda
    assert all(t not in agenda for t in (lirmm, st, conf, late_conf))
    _no_text_in_audit(public, conf, late_public, late_conf)


# =========================================================================== duplicate


def test_duplicate_copies_the_frame_only(team: SimpleNamespace) -> None:
    title = marker()
    meeting = meeting_of(
        team,
        kind=Meeting.Kind.PRESENTATION,
        presentation_venue=Meeting.Venue.ST_MEETING,
        level=L.CONFIDENTIAL,
        title=title,
        location=Meeting.Location.ROUSSET,
        location_detail="Salle fictive",
        visio_link="https://visio.example.org/fictif",
        duration_minutes=45,
        attendees=[team.student, team.tutor],
    )
    services.save_agenda(meeting=meeting, user=team.student, agenda="Ordre du jour fictif")
    _minutes(team, meeting)
    services.add_decision(meeting=meeting, user=team.student, text="Décision fictive")
    services.link_document(meeting=meeting, user=team.student, document=_store(team))
    later = START + dt.timedelta(days=14)
    copy = services.duplicate_meeting(meeting=meeting, user=team.tutor, starts_at=later)
    assert (copy.kind, copy.presentation_venue, copy.location, copy.duration_minutes) == (
        "PRESENTATION",
        "ST_MEETING",
        "ROUSSET",
        45,
    )
    assert (copy.level, copy.starts_at, copy.minutes_version) == ("CONFIDENTIAL", later, 0)
    assert {a.user for a in copy.attendees.all()} == {team.student, team.tutor}
    texts = services.meeting_detail(team.student, copy).texts
    assert texts["title"] == title
    assert texts["agenda"] == "" and texts["minutes"] == ""
    assert texts["location_detail"] == "" and texts["visio_link"] == ""
    assert not copy.decisions.exists() and not copy.document_links.exists()
    with pytest.raises(ValidationError) as exc:
        services.duplicate_meeting(
            meeting=meeting, user=team.student, starts_at=dt.datetime(2031, 1, 1, tzinfo=dt.UTC)
        )
    assert _code(exc) == "out_of_bounds"
    (dup,) = _details(AuditAction.MEETING_DUPLICATED)
    assert dup["source_id"] == str(meeting.pk)


# =========================================================================== documents


def test_documents_linked_are_shown_to_those_who_may_read_them(team: SimpleNamespace) -> None:
    meeting = meeting_of(team, attendees=[team.student, team.tutor])
    st_doc = _store(team, L.INTERNAL_ST)
    link = services.link_document(meeting=meeting, user=team.student, document=st_doc)
    assert services.meeting_detail(team.tutor, meeting).documents == [st_doc]
    hidden = services.meeting_detail(team.director, meeting)
    assert hidden.documents == [] and hidden.hidden_documents == 1
    with pytest.raises(ValidationError) as exc:
        services.link_document(meeting=meeting, user=team.student, document=st_doc)
    assert _code(exc) == "already_linked"
    services.unlink_document(link=link, user=team.tutor)
    assert not meeting.document_links.exists()


def test_a_document_must_be_readable_and_of_the_same_thesis(team: SimpleNamespace) -> None:
    meeting = meeting_of(team, attendees=[team.student, team.tutor])
    lirmm = _store(team, L.INTERNAL_LIRMM)
    with pytest.raises(PermissionDenied):
        services.link_document(meeting=meeting, user=team.tutor, document=lirmm)
    other = make_team()
    with pytest.raises(PermissionDenied):
        services.link_document(
            meeting=meeting, user=team.student, document=_store(other, thesis=other.thesis)
        )


# =========================================================================== thesis state


def test_a_finished_thesis_freezes_its_meetings(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    team.thesis.status = team.thesis.Status.DEFENDED
    team.thesis.save()
    with pytest.raises(ValidationError) as exc:
        services.save_agenda(meeting=meeting, user=team.student, agenda="Trop tard")
    assert _code(exc) == "frozen"


def test_an_unrelated_thesis_cannot_be_used(team: SimpleNamespace) -> None:
    thesis = ThesisFactory.create()
    with pytest.raises(PermissionDenied):
        meeting_of(team, thesis=thesis)
