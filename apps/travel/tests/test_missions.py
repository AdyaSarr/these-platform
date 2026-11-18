"""Missions (M7, step 3): the student plans them; texts sealed at the mission's level;
payer => level (the payer's person reads what they will grant); each role sees the
missions it needs (a co-supervisor: LIRMM only, a hidden one looks unknown);
presence days filled without overwriting."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import Thesis
from apps.conferences.tests.helpers import edition_of, submission_of
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.meetings.tests.helpers import meeting_of
from apps.travel import services
from apps.travel.models import Mission, PresenceDay
from apps.travel.tests.helpers import NOW, D, database_dump, make_team, marker, mission_of
from apps.vault import services as vault
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=False)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _events(action: str) -> list[dict[str, Any]]:
    return [e.details_json for e in AuditEvent.objects.filter(action=action).order_by("seq")]


def _texts(user: Any, mission: Mission) -> dict[str, Any]:
    return services.mission_texts(user, mission)


# =========================================================================== creation


def test_the_student_plans_a_mission(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    assert mission.status == "PLANNED"
    assert (mission.payer, mission.level) == ("ST", "INTERNAL_ST")
    assert mission.transports == ["TRAIN"]
    assert mission.lodging == "NONE"
    assert _events("MISSION_CREATED") == [
        {
            "destination": "LIRMM",
            "payer": "ST",
            "level": "INTERNAL_ST",
            "start_date": "2027-02-08",
            "end_date": "2027-02-10",
        }
    ]


@pytest.mark.parametrize(
    ("payer", "level"), [("LIRMM", "INTERNAL_LIRMM"), ("OTHER", "INTERNAL_LIRMM")]
)
def test_default_level_follows_the_payer(team: SimpleNamespace, payer: str, level: str) -> None:
    assert mission_of(team, payer=payer).level == level


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor", "csi", "guest", "outsider"])
def test_only_the_student_plans(team: SimpleNamespace, role: str) -> None:
    with pytest.raises(PermissionDenied):
        mission_of(team, user=getattr(team, role))
    assert not Mission.objects.exists()


@pytest.mark.parametrize(
    ("payer", "level", "ok"),
    [
        ("ST", L.PUBLIC, True),
        ("ST", L.INTERNAL_ST, True),
        ("ST", L.CONFIDENTIAL, True),
        ("ST", L.INTERNAL_LIRMM, False),  # the tutor could not read what they grant
        ("LIRMM", L.PUBLIC, True),
        ("LIRMM", L.INTERNAL_LIRMM, True),
        ("LIRMM", L.CONFIDENTIAL, True),
        ("LIRMM", L.INTERNAL_ST, False),  # the director could not read it
        ("OTHER", L.INTERNAL_ST, True),
        ("OTHER", L.INTERNAL_LIRMM, True),
    ],
)
def test_payer_bounds_the_level(team: SimpleNamespace, payer: str, level: L, ok: bool) -> None:
    if ok:
        assert mission_of(team, payer=payer, level=level).level == level.value
    else:
        with pytest.raises(ValidationError) as info:
            mission_of(team, payer=payer, level=level)
        assert info.value.code == "payer_level"


def test_texts_are_sealed_never_in_clear(team: SimpleNamespace) -> None:
    secrets = {"destination_detail": marker(), "purpose": marker(), "lodging_note": marker()}
    mission = mission_of(team, destination="OTHER", lodging="HOTEL", **secrets)
    dump = "\n".join(database_dump())
    for value in secrets.values():
        assert value not in dump
    assert _texts(team.student, mission) == secrets


@pytest.mark.parametrize(
    "fields",
    [
        {"end_date": D(2027, 2, 7)},  # before the start
        {"end_date": D(2027, 4, 9)},  # 61 days
        {"start_date": D(2026, 8, 31), "end_date": D(2026, 9, 1)},  # before the bounds
        {"destination": "HOME"},
        {"payer": "UNIVERSITY"},
        {"transports": []},
        {"transports": ["CAR"]},  # no car
        {"transports": ["TRAIN", "TRAIN"]},
        {"lodging": "TENT"},
        {"destination": "OTHER"},  # a place is needed
        {"destination": "LIRMM", "destination_detail": "Montpellier"},  # only for OTHER
        {"purpose": "x" * 2001},
        {"purpose": "fictif\x00"},
        {"destination_detail": "un\ndeux", "destination": "OTHER"},  # one line
    ],
)
def test_invalid_missions(team: SimpleNamespace, fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        mission_of(team, **fields)
    assert not Mission.objects.exists()


def test_a_60_day_mission_is_the_longest(team: SimpleNamespace) -> None:
    mission_of(team, start_date=D(2027, 2, 8), end_date=D(2027, 4, 8))


def test_several_transports_in_order(team: SimpleNamespace) -> None:
    mission = mission_of(team, transports=["TRAIN", "URBAN_TRANSIT", "BIKE"])
    assert mission.transports == ["TRAIN", "URBAN_TRANSIT", "BIKE"]


# =========================================================================== links


def test_a_conference_mission_links_an_edition(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    mission = mission_of(team, destination="CONFERENCE", payer="LIRMM", edition=edition)
    assert mission.edition_id == edition.pk


def test_an_edition_only_for_a_conference(team: SimpleNamespace) -> None:
    with pytest.raises(ValidationError):
        mission_of(team, destination="LIRMM", edition=edition_of(team))


def test_links_to_a_meeting_and_a_submission(team: SimpleNamespace) -> None:
    meeting, submission = meeting_of(team), submission_of(team)
    mission = mission_of(team, meeting=meeting, submission=submission)
    assert (mission.meeting_id, mission.submission_id) == (meeting.pk, submission.pk)


@pytest.mark.parametrize("what", ["edition", "meeting", "submission"])
def test_links_of_another_thesis_are_refused(team: SimpleNamespace, what: str) -> None:
    other = make_team()
    theirs = {
        "edition": lambda: edition_of(other),
        "meeting": lambda: meeting_of(other),
        "submission": lambda: submission_of(other),
    }[what]()
    extra = {"destination": "CONFERENCE"} if what == "edition" else {}
    with pytest.raises(ValidationError):
        mission_of(team, **{what: theirs}, **extra)
    assert not Mission.objects.exists()


# =========================================================================== presence


def test_presence_days_are_filled_without_overwriting(team: SimpleNamespace) -> None:
    PresenceDay.objects.create(thesis=team.thesis, day=D(2027, 2, 9), kind="REMOTE")
    # Fri 5 to Tue 9 Feb: 5, 8 and 9 are working days; the 9th is kept.
    mission_of(team, start_date=D(2027, 2, 5), end_date=D(2027, 2, 9), fill_presence=True)
    days = dict(PresenceDay.objects.filter(thesis=team.thesis).values_list("day", "kind"))
    assert days == {D(2027, 2, 5): "LIRMM", D(2027, 2, 8): "LIRMM", D(2027, 2, 9): "REMOTE"}


@pytest.mark.parametrize(
    ("destination", "kind"),
    [("LIRMM", "LIRMM"), ("ROUSSET", "ROUSSET"), ("CONFERENCE", "CONFERENCE"), ("OTHER", None)],
)
def test_presence_kind_of_each_destination(
    team: SimpleNamespace, destination: str, kind: str | None
) -> None:
    extra = {"destination_detail": "Ville fictive"} if destination == "OTHER" else {}
    mission_of(team, destination=destination, fill_presence=True, **extra)
    kinds = set(PresenceDay.objects.filter(thesis=team.thesis).values_list("kind", flat=True))
    assert kinds == ({kind} if kind else set())


def test_no_presence_unless_asked(team: SimpleNamespace) -> None:
    mission_of(team, fill_presence=False)
    assert not PresenceDay.objects.exists()


def test_past_days_filled_by_a_mission_are_journaled(team: SimpleNamespace) -> None:
    mission_of(team, start_date=D(2027, 1, 18), end_date=D(2027, 1, 21), fill_presence=True)
    assert [e["day"] for e in _events("PRESENCE_CORRECTED")] == ["2027-01-18", "2027-01-19"]


# =========================================================================== update


def test_update_texts_and_dates(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    changed = services.update_mission(
        mission=mission,
        user=team.student,
        changes={"end_date": D(2027, 2, 12), "purpose": "Autre motif fictif.", "lodging": "HOTEL"},
    )
    assert sorted(changed) == ["end_date", "lodging", "purpose"]
    mission.refresh_from_db()
    assert (mission.end_date, mission.lodging) == (D(2027, 2, 12), "HOTEL")
    assert _texts(team.student, mission)["purpose"] == "Autre motif fictif."
    assert _events("MISSION_UPDATED") == [{"fields": ["end_date", "lodging", "purpose"]}]


def test_a_payer_change_keeps_payer_and_level_consistent(team: SimpleNamespace) -> None:
    mission = mission_of(team, payer="ST", level=L.INTERNAL_ST)
    with pytest.raises(ValidationError) as info:
        services.update_mission(mission=mission, user=team.student, changes={"payer": "LIRMM"})
    assert info.value.code == "payer_level"
    public = mission_of(team, payer="ST", level=L.PUBLIC)
    services.update_mission(mission=public, user=team.student, changes={"payer": "LIRMM"})


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor"])
def test_only_the_student_updates(team: SimpleNamespace, role: str) -> None:
    mission = mission_of(team)
    with pytest.raises(PermissionDenied):
        services.update_mission(
            mission=mission, user=getattr(team, role), changes={"end_date": D(2027, 2, 12)}
        )


def test_unknown_fields_cannot_be_changed(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    for field in ("status", "level", "thesis"):
        with pytest.raises(ValueError):
            services.update_mission(mission=mission, user=team.student, changes={field: "x"})


# =========================================================================== level


def test_level_change_asks_for_the_second_factor_and_reseals(team: SimpleNamespace) -> None:
    mission = mission_of(team, purpose=marker())
    purpose = _texts(team.student, mission)["purpose"]
    with pytest.raises(ReauthenticationRequiredError):
        services.change_mission_level(
            mission=mission, user=team.student, level=L.CONFIDENTIAL, recent_mfa=False
        )
    services.change_mission_level(
        mission=mission, user=team.student, level=L.CONFIDENTIAL, recent_mfa=True
    )
    mission.refresh_from_db()
    assert mission.level == "CONFIDENTIAL"
    assert _texts(team.student, mission)["purpose"] == purpose
    assert _texts(team.director, mission)["purpose"] == purpose  # CONFIDENTIAL: director reads
    assert _events("MISSION_LEVEL_CHANGED") == [{"from": "INTERNAL_ST", "to": "CONFIDENTIAL"}]


def test_level_change_respects_the_payer(team: SimpleNamespace) -> None:
    mission = mission_of(team, payer="ST")
    with pytest.raises(ValidationError):
        services.change_mission_level(
            mission=mission, user=team.student, level=L.INTERNAL_LIRMM, recent_mfa=True
        )


@pytest.mark.parametrize("role", ["director", "tutor"])
def test_only_the_student_changes_the_level(team: SimpleNamespace, role: str) -> None:
    mission = mission_of(team)
    with pytest.raises(PermissionDenied):
        services.change_mission_level(
            mission=mission, user=getattr(team, role), level=L.PUBLIC, recent_mfa=False
        )


# =========================================================================== status


def test_cancel(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    services.cancel_mission(mission=mission, user=team.student)
    mission.refresh_from_db()
    assert mission.status == "CANCELLED"
    assert _events("MISSION_STATUS_CHANGED") == [{"from": "PLANNED", "to": "CANCELLED"}]
    for again in (
        lambda: services.cancel_mission(mission=mission, user=team.student),
        lambda: services.update_mission(
            mission=mission, user=team.student, changes={"purpose": "x"}
        ),
    ):
        with pytest.raises(ValidationError) as info:
            again()
        assert info.value.code == "terminal"


def test_done_after_the_return_for_another_payer(team: SimpleNamespace, time_machine: Any) -> None:
    mission = mission_of(team, payer="OTHER", end_date=D(2027, 2, 10))
    with pytest.raises(ValidationError) as info:
        services.mark_done(mission=mission, user=team.student)
    assert info.value.code == "not_returned"
    time_machine.move_to(dt.datetime(2027, 2, 10, 9, 0, tzinfo=dt.UTC), tick=False)
    services.mark_done(mission=mission, user=team.student)
    mission.refresh_from_db()
    assert mission.status == "DONE"


@pytest.mark.parametrize("payer", ["ST", "LIRMM"])
def test_done_needs_a_mission_order_for_st_and_lirmm(
    team: SimpleNamespace, payer: str, time_machine: Any
) -> None:
    mission = mission_of(team, payer=payer)
    time_machine.move_to(dt.datetime(2027, 3, 1, 9, 0, tzinfo=dt.UTC), tick=False)
    with pytest.raises(ValidationError) as info:
        services.mark_done(mission=mission, user=team.student)
    assert info.value.code == "order_required"


@pytest.mark.parametrize("status", [Thesis.Status.DEFENDED, Thesis.Status.CLOSED])
def test_a_finished_thesis_is_frozen(team: SimpleNamespace, status: str) -> None:
    mission = mission_of(team)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=status)
    if status == Thesis.Status.CLOSED:  # M13a: the status gate of can() refuses first
        with pytest.raises(PermissionDenied):
            mission_of(team)
        with pytest.raises(PermissionDenied):
            services.cancel_mission(mission=mission, user=team.student)
        return
    with pytest.raises(ValidationError) as info:
        mission_of(team)
    assert info.value.code == "frozen"
    with pytest.raises(ValidationError):
        services.cancel_mission(mission=mission, user=team.student)


# =========================================================================== reading


def _world(team: SimpleNamespace) -> dict[str, Mission]:
    return {
        "lirmm": mission_of(team, destination="LIRMM", payer="LIRMM"),
        "lirmm_st": mission_of(team, destination="LIRMM", payer="ST"),
        "rousset": mission_of(team, destination="ROUSSET", payer="ST"),
        "conference": mission_of(team, destination="CONFERENCE", payer="LIRMM"),
        "other": mission_of(team, destination="OTHER", destination_detail="Ville", payer="OTHER"),
    }


@pytest.mark.parametrize("role", ["student", "director", "tutor"])
def test_these_roles_see_every_mission(team: SimpleNamespace, role: str) -> None:
    missions = _world(team)
    listed = {m.pk for m in services.mission_list(getattr(team, role), team.thesis)}
    assert listed == {m.pk for m in missions.values()}


def test_the_co_supervisor_sees_lirmm_missions_only(team: SimpleNamespace) -> None:
    missions = _world(team)
    listed = {m.pk for m in services.mission_list(team.co_supervisor, team.thesis)}
    assert listed == {missions["lirmm"].pk, missions["lirmm_st"].pk}


def test_a_hidden_mission_looks_unknown(team: SimpleNamespace) -> None:
    import uuid

    missions = _world(team)
    for mission_id in (missions["rousset"].pk, uuid.uuid4()):
        with pytest.raises(PermissionDenied):
            services.mission_for(team.co_supervisor, team.thesis, mission_id, operation="read")
    reasons = [e["reason"] for e in _events("TRAVEL_ACCESS_DENIED")]
    assert reasons == ["unknown_mission", "unknown_mission"]
    found = services.mission_for(team.co_supervisor, team.thesis, missions["lirmm"].pk, "read")
    assert found.pk == missions["lirmm"].pk


@pytest.mark.parametrize("role", ["csi", "guest", "outsider"])
def test_outside_the_supervision_no_mission(team: SimpleNamespace, role: str) -> None:
    _world(team)
    with pytest.raises(PermissionDenied):
        services.mission_list(getattr(team, role), team.thesis)


def test_a_mission_of_another_thesis_is_unknown(team: SimpleNamespace) -> None:
    other = make_team()
    theirs = mission_of(other)
    with pytest.raises(PermissionDenied):
        services.mission_for(team.student, team.thesis, theirs.pk, operation="read")


def test_texts_follow_the_level(team: SimpleNamespace) -> None:
    lab = mission_of(team, destination="LIRMM", payer="LIRMM", purpose="Motif labo fictif.")
    st = mission_of(team, destination="LIRMM", payer="ST", purpose="Motif ST fictif.")
    assert _texts(team.co_supervisor, lab)["purpose"] == "Motif labo fictif."
    assert _texts(team.co_supervisor, st)["purpose"] is vault.RESTRICTED
    assert _texts(team.tutor, lab)["purpose"] is vault.RESTRICTED
    assert _texts(team.tutor, st)["purpose"] == "Motif ST fictif."
    assert _texts(team.director, st)["purpose"] is vault.RESTRICTED
