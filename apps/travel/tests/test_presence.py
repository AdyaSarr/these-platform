"""Presence calendar services (M7): the student writes, a past day changed is
journaled, each role sees only the kinds of day it needs (visible_to), and the
counters shown to a role are computed from what that role sees."""

from __future__ import annotations

import datetime as dt
from fractions import Fraction
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import Thesis
from apps.travel import services
from apps.travel.models import PresenceDay
from apps.travel.tests.helpers import NOW, TODAY, D, make_team
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=False)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _days(team: SimpleNamespace) -> dict[dt.date, str]:
    return dict(PresenceDay.objects.filter(thesis=team.thesis).values_list("day", "kind"))


def _set(team: SimpleNamespace, first: dt.date, last: dt.date, kind: str | None, **kw: Any) -> int:
    return services.set_presence(
        thesis=team.thesis, user=team.student, first=first, last=last, kind=kind, **kw
    )


def _corrections() -> list[dict[str, Any]]:
    return [
        e.details_json
        for e in AuditEvent.objects.filter(action="PRESENCE_CORRECTED").order_by("seq")
    ]


# =========================================================================== writing


def test_a_range_sets_working_days_only(team: SimpleNamespace) -> None:
    # Mon 25 Jan to Sun 7 Feb 2027: 10 working days.
    assert _set(team, D(2027, 1, 25), D(2027, 2, 7), "LIRMM") == 10
    days = _days(team)
    assert len(days) == 10
    assert all(d.weekday() < 5 for d in days)
    assert set(days.values()) == {"LIRMM"}


def test_a_non_working_day_on_request(team: SimpleNamespace) -> None:
    assert _set(team, D(2027, 1, 30), D(2027, 1, 30), "CONFERENCE") == 0  # a Saturday
    assert _set(team, D(2027, 1, 30), D(2027, 1, 30), "CONFERENCE", include_non_working=True) == 1
    assert _days(team) == {D(2027, 1, 30): "CONFERENCE"}


def test_whit_monday_follows_the_policy(team: SimpleNamespace) -> None:
    assert _set(team, D(2027, 5, 17), D(2027, 5, 17), "LIRMM") == 0
    services.set_rules(
        thesis=team.thesis, user=team.director, recent_mfa=True, whit_monday_holiday=False
    )
    assert _set(team, D(2027, 5, 17), D(2027, 5, 17), "LIRMM") == 1


def test_changing_and_clearing_days(team: SimpleNamespace) -> None:
    _set(team, D(2027, 1, 25), D(2027, 1, 29), "LIRMM")
    assert _set(team, D(2027, 1, 27), D(2027, 1, 28), "REMOTE") == 2
    assert _set(team, D(2027, 1, 25), D(2027, 1, 29), "REMOTE") == 3  # 2 already REMOTE
    assert _set(team, D(2027, 1, 28), D(2027, 1, 29), None) == 2
    assert _days(team) == {
        D(2027, 1, 25): "REMOTE",
        D(2027, 1, 26): "REMOTE",
        D(2027, 1, 27): "REMOTE",
    }


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor", "csi", "guest", "outsider"])
def test_only_the_student_writes(team: SimpleNamespace, role: str) -> None:
    with pytest.raises(PermissionDenied):
        services.set_presence(
            thesis=team.thesis,
            user=getattr(team, role),
            first=D(2027, 1, 25),
            last=D(2027, 1, 25),
            kind="LIRMM",
        )
    assert not PresenceDay.objects.exists()


@pytest.mark.parametrize(
    ("first", "last"),
    [
        (D(2027, 1, 29), D(2027, 1, 25)),  # reversed
        (D(2027, 1, 1), D(2027, 4, 3)),  # 93 days
        (D(2026, 8, 31), D(2026, 9, 1)),  # before cifre_start - 3 months
        (D(2030, 11, 29), D(2030, 12, 2)),  # after planned_end + 12 months
    ],
)
def test_range_limits(team: SimpleNamespace, first: dt.date, last: dt.date) -> None:
    with pytest.raises(ValidationError):
        _set(team, first, last, "LIRMM")
    assert not PresenceDay.objects.exists()


def test_92_days_is_the_longest_range(team: SimpleNamespace) -> None:
    _set(team, D(2027, 1, 1), D(2027, 4, 2), "ROUSSET")


@pytest.mark.parametrize("kind", ["", "HOME", "UNAVAILABLE", "lirmm"])
def test_unknown_kind_is_refused(team: SimpleNamespace, kind: str) -> None:
    with pytest.raises(ValidationError):
        _set(team, D(2027, 1, 25), D(2027, 1, 25), kind)


def test_a_datetime_is_refused(team: SimpleNamespace) -> None:
    with pytest.raises(TypeError):
        _set(team, NOW, D(2027, 1, 25), "LIRMM")  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [Thesis.Status.DEFENDED, Thesis.Status.CLOSED])
def test_a_finished_thesis_is_frozen(team: SimpleNamespace, status: str) -> None:
    Thesis.objects.filter(pk=team.thesis.pk).update(status=status)
    with pytest.raises(ValidationError) as info:
        _set(team, D(2027, 1, 25), D(2027, 1, 25), "LIRMM")
    assert info.value.code == "frozen"


# =========================================================================== past days


def test_future_days_change_without_journal(team: SimpleNamespace) -> None:
    _set(team, TODAY, D(2027, 1, 29), "LIRMM")  # today is not past
    _set(team, TODAY, D(2027, 1, 29), "LEAVE")
    _set(team, TODAY, D(2027, 1, 29), None)
    assert _corrections() == []


def test_every_change_of_a_past_day_is_journaled(team: SimpleNamespace) -> None:
    monday = D(2027, 1, 18)
    _set(team, monday, monday, "LIRMM")  # filled in late
    _set(team, monday, monday, "LIRMM")  # unchanged: nothing
    _set(team, monday, monday, "LEAVE")
    _set(team, monday, monday, None)
    events = AuditEvent.objects.filter(action="PRESENCE_CORRECTED").order_by("seq")
    assert [
        (e.details_json["day"], e.details_json["from"], e.details_json["to"]) for e in events
    ] == [
        ("2027-01-18", None, "LIRMM"),
        ("2027-01-18", "LIRMM", "LEAVE"),
        ("2027-01-18", "LEAVE", None),
    ]
    assert {e.actor_id for e in events} == {team.student.pk}
    assert {str(e.thesis_id) for e in events} == {str(team.thesis.pk)}


def test_one_event_per_past_day_of_a_range(team: SimpleNamespace) -> None:
    # Thu 14 to Wed 20 Jan: 14, 15, 18, 19 are past working days; 20 is today.
    _set(team, D(2027, 1, 14), TODAY, "ROUSSET")
    assert [c["day"] for c in _corrections()] == [
        "2027-01-14",
        "2027-01-15",
        "2027-01-18",
        "2027-01-19",
    ]


def test_past_means_before_today_in_paris(team: SimpleNamespace, time_machine: Any) -> None:
    # 23:30 UTC on 19 Jan is 00:30 on 20 Jan in Paris: the 19th is already past.
    time_machine.move_to(dt.datetime(2027, 1, 19, 23, 30, tzinfo=dt.UTC), tick=False)
    _set(team, D(2027, 1, 19), D(2027, 1, 20), "LIRMM")
    assert [c["day"] for c in _corrections()] == ["2027-01-19"]


# =========================================================================== visible_to

WEEK = {
    D(2027, 1, 4): "ROUSSET",
    D(2027, 1, 5): "LIRMM",
    D(2027, 1, 6): "REMOTE",
    D(2027, 1, 7): "CONFERENCE",
    D(2027, 1, 8): "LEAVE",
}


def _fill_week(team: SimpleNamespace) -> None:
    for day, kind in WEEK.items():
        PresenceDay.objects.create(thesis=team.thesis, day=day, kind=kind)


def _visible(team: SimpleNamespace, role: str) -> dict[dt.date, str]:
    return services.visible_days(getattr(team, role), team.thesis, D(2027, 1, 1), D(2027, 1, 31))


@pytest.mark.parametrize("role", ["student", "tutor"])
def test_the_student_and_the_employer_see_every_kind(team: SimpleNamespace, role: str) -> None:
    _fill_week(team)
    assert _visible(team, role) == WEEK


def test_the_director_sees_leave_as_unavailable(team: SimpleNamespace) -> None:
    _fill_week(team)
    assert _visible(team, "director") == {**WEEK, D(2027, 1, 8): "UNAVAILABLE"}


def test_the_co_supervisor_sees_lirmm_days_only(team: SimpleNamespace) -> None:
    _fill_week(team)
    assert _visible(team, "co_supervisor") == {D(2027, 1, 5): "LIRMM"}


def test_the_manager_filters_rows_for_the_co_supervisor(team: SimpleNamespace) -> None:
    _fill_week(team)
    rows = PresenceDay.objects.visible_to(team.co_supervisor, team.thesis)
    assert set(rows.values_list("kind", flat=True)) == {"LIRMM"}
    assert PresenceDay.objects.visible_to(team.tutor, team.thesis).count() == 5
    assert not PresenceDay.objects.visible_to(team.csi, team.thesis).exists()


@pytest.mark.parametrize("role", ["csi", "guest", "outsider"])
def test_outside_the_supervision_nothing_is_visible(team: SimpleNamespace, role: str) -> None:
    _fill_week(team)
    with pytest.raises(PermissionDenied):
        _visible(team, role)


def test_another_thesis_days_are_never_visible(team: SimpleNamespace) -> None:
    other = make_team()
    PresenceDay.objects.create(thesis=other.thesis, day=D(2027, 1, 5), kind="LIRMM")
    assert _visible(team, "student") == {}


def test_a_user_with_several_roles_gets_the_widest_view(team: SimpleNamespace) -> None:
    from apps.accounts.roles import Role
    from apps.accounts.tests.factories import MembershipFactory

    _fill_week(team)
    MembershipFactory.create(thesis=team.thesis, user=team.co_supervisor, role=Role.DIRECTOR)
    assert _visible(team, "co_supervisor") == {**WEEK, D(2027, 1, 8): "UNAVAILABLE"}


# =========================================================================== counters


def _summary(team: SimpleNamespace, role: str) -> services.SplitSummary:
    return services.split_summary(getattr(team, role), team.thesis)


def test_full_counter_for_the_student_and_the_tutor(team: SimpleNamespace) -> None:
    _fill_week(team)
    services.set_target(
        thesis=team.thesis, user=team.tutor, recent_mfa=True, cifre_year=1, st_percent=70
    )
    for role in ("student", "tutor"):
        summary = _summary(team, role)
        assert summary.cifre_year == 1
        assert (summary.first, summary.last) == (D(2026, 12, 1), TODAY)
        assert (summary.st_days, summary.lab_days) == (2, 2)  # Rousset + remote; LIRMM + conf.
        assert summary.st_percent == 50
        assert summary.target_st_percent == 70
        assert summary.gap == 20
        assert summary.alert is True
        assert summary.shown_counts == {
            "ROUSSET": 1,
            "LIRMM": 1,
            "REMOTE": 1,
            "CONFERENCE": 1,
            "LEAVE": 1,
        }
        assert summary.lirmm_days == 1
        # 1 Dec 2026 .. 20 Jan 2027: 35 working days (25 Dec, 1 Jan off), 5 filled in.
        assert summary.working_days == 35
        assert summary.unfilled_days == 30


def test_director_counter_shows_unavailable_not_leave(team: SimpleNamespace) -> None:
    _fill_week(team)
    summary = _summary(team, "director")
    assert (summary.st_days, summary.lab_days, summary.st_percent) == (2, 2, 50)
    assert summary.shown_counts == {
        "ROUSSET": 1,
        "LIRMM": 1,
        "REMOTE": 1,
        "CONFERENCE": 1,
        "UNAVAILABLE": 1,
    }
    assert summary.unfilled_days == 30


def test_co_supervisor_counter_is_lirmm_days_only(team: SimpleNamespace) -> None:
    _fill_week(team)
    services.set_target(
        thesis=team.thesis, user=team.director, recent_mfa=True, cifre_year=1, st_percent=70
    )
    summary = _summary(team, "co_supervisor")
    assert summary.lirmm_days == 1
    assert summary.shown_counts == {"LIRMM": 1}
    for hidden in (
        "st_days",
        "lab_days",
        "st_percent",
        "target_st_percent",
        "gap",
        "alert",
        "working_days",
        "unfilled_days",
    ):
        assert getattr(summary, hidden) is None, hidden


def test_counter_follows_the_rules(team: SimpleNamespace) -> None:
    _fill_week(team)
    services.set_rules(thesis=team.thesis, user=team.tutor, recent_mfa=True, remote_counts_as="LAB")
    summary = _summary(team, "student")
    assert (summary.st_days, summary.lab_days) == (1, 3)
    assert summary.st_percent == 25


def test_counter_of_a_whole_past_year(team: SimpleNamespace, time_machine: Any) -> None:
    time_machine.move_to(dt.datetime(2028, 1, 10, 9, 0, tzinfo=dt.UTC), tick=False)
    _fill_week(team)
    summary = services.split_summary(team.student, team.thesis, cifre_year=1)
    assert (summary.first, summary.last) == (D(2026, 12, 1), D(2027, 11, 30))
    assert summary.st_percent == Fraction(50)
    assert services.split_summary(team.student, team.thesis).cifre_year == 2


@pytest.mark.parametrize("year", [0, 4, 99])
def test_counter_of_a_year_outside_the_thesis(team: SimpleNamespace, year: int) -> None:
    with pytest.raises(ValidationError):
        services.split_summary(team.student, team.thesis, cifre_year=year)


# =========================================================================== month view


def test_month_view_grid(team: SimpleNamespace) -> None:
    _fill_week(team)
    month = services.month_view(team.student, team.thesis, 2027, 1)
    # January 2027 starts on a Friday: weeks start on Monday.
    assert [c.day if c else None for c in month.weeks[0]] == [
        None,
        None,
        None,
        None,
        D(2027, 1, 1),
        D(2027, 1, 2),
        D(2027, 1, 3),
    ]
    cells = {c.day: c for week in month.weeks for c in week if c}
    assert len(cells) == 31
    assert cells[D(2027, 1, 1)].holiday and not cells[D(2027, 1, 1)].working
    assert cells[D(2027, 1, 2)].working is False
    assert cells[TODAY].today and not cells[D(2027, 1, 19)].today
    assert cells[D(2027, 1, 8)].kind == "LEAVE"
    assert cells[D(2027, 1, 11)].kind is None
    assert (month.previous, month.next) == ((2026, 12), (2027, 2))
    assert month.lirmm_days == 1


def test_month_view_masks_per_role(team: SimpleNamespace) -> None:
    _fill_week(team)
    director = services.month_view(team.director, team.thesis, 2027, 1)
    co_sup = services.month_view(team.co_supervisor, team.thesis, 2027, 1)
    kinds = lambda m: {c.day: c.kind for w in m.weeks for c in w if c and c.kind}  # noqa: E731
    assert kinds(director)[D(2027, 1, 8)] == "UNAVAILABLE"
    assert kinds(co_sup) == {D(2027, 1, 5): "LIRMM"}


def test_month_rule(team: SimpleNamespace) -> None:
    _fill_week(team)
    assert services.month_view(team.student, team.thesis, 2027, 1).rule_met is None
    services.set_rules(
        thesis=team.thesis, user=team.director, recent_mfa=True, min_lab_days_per_month=2
    )
    month = services.month_view(team.co_supervisor, team.thesis, 2027, 1)
    assert (month.rule_minimum, month.lirmm_days, month.rule_met) == (2, 1, False)
    PresenceDay.objects.create(thesis=team.thesis, day=D(2027, 1, 12), kind="LIRMM")
    assert services.month_view(team.co_supervisor, team.thesis, 2027, 1).rule_met is True


@pytest.mark.parametrize(("year", "month"), [(2026, 8), (2030, 12), (2027, 13), (2027, 0)])
def test_month_outside_the_bounds(team: SimpleNamespace, year: int, month: int) -> None:
    with pytest.raises(ValidationError):
        services.month_view(team.student, team.thesis, year, month)


def test_first_and_last_months_have_no_way_further(team: SimpleNamespace) -> None:
    assert services.month_view(team.student, team.thesis, 2026, 9).previous is None
    assert services.month_view(team.student, team.thesis, 2030, 11).next is None
