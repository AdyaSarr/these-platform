"""Database constraints of the travel module (M7): each CHECK and UNIQUE is hit in
SQL, bypassing the services."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

import pytest
from django.db import IntegrityError, transaction

from apps.accounts.tests.factories import ThesisFactory
from apps.travel.models import PresenceDay, TimeSplitTarget, TravelPolicy
from apps.travel.tests.helpers import D

pytestmark = pytest.mark.django_db

NOW_DT = dt.datetime(2027, 1, 20, 9, 0, tzinfo=dt.UTC)


def _refused(create: Callable[[], Any]) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        create()


def test_one_presence_row_per_thesis_and_day() -> None:
    thesis = ThesisFactory.create()
    PresenceDay.objects.create(thesis=thesis, day=D(2027, 1, 4), kind="LIRMM")
    _refused(lambda: PresenceDay.objects.create(thesis=thesis, day=D(2027, 1, 4), kind="LEAVE"))
    other = ThesisFactory.create()
    PresenceDay.objects.create(thesis=other, day=D(2027, 1, 4), kind="LIRMM")


@pytest.mark.parametrize("kind", ["", "HOME", "UNAVAILABLE", "lirmm"])
def test_presence_kind_is_a_closed_list(kind: str) -> None:
    thesis = ThesisFactory.create()
    _refused(lambda: PresenceDay.objects.create(thesis=thesis, day=D(2027, 1, 4), kind=kind))


@pytest.mark.parametrize(("st", "lab"), [(60, 30), (101, -1), (-1, 101), (0, 99)])
def test_target_percents_add_up_to_100(st: int, lab: int) -> None:
    thesis = ThesisFactory.create()
    _refused(
        lambda: TimeSplitTarget.objects.create(
            thesis=thesis, cifre_year=1, st_percent=st, lab_percent=lab
        )
    )


def test_one_target_per_cifre_year_numbered_from_1() -> None:
    thesis = ThesisFactory.create()
    TimeSplitTarget.objects.create(thesis=thesis, cifre_year=1, st_percent=50, lab_percent=50)
    _refused(
        lambda: TimeSplitTarget.objects.create(
            thesis=thesis, cifre_year=1, st_percent=60, lab_percent=40
        )
    )
    _refused(
        lambda: TimeSplitTarget.objects.create(
            thesis=thesis, cifre_year=0, st_percent=60, lab_percent=40
        )
    )


def test_policy_defaults() -> None:
    policy = TravelPolicy.objects.create(thesis=ThesisFactory.create())
    assert policy.remote_counts_as == "ST"
    assert policy.conference_counts_as == "LAB"
    assert policy.whit_monday_holiday is True
    assert policy.gap_alert_points == 10
    assert policy.min_lab_days_per_month is None
    assert policy.order_lead_days == 15
    assert policy.receipt_grace_days == 3


def test_one_policy_per_thesis() -> None:
    thesis = ThesisFactory.create()
    TravelPolicy.objects.create(thesis=thesis)
    _refused(lambda: TravelPolicy.objects.create(thesis=thesis))


@pytest.mark.parametrize(
    "fields",
    [
        {"remote_counts_as": "XX"},  # short: hits the CHECK, not the length
        {"conference_counts_as": ""},
        {"gap_alert_points": 0},
        {"gap_alert_points": 51},
        {"min_lab_days_per_month": 24},
        {"order_lead_days": 91},
        {"receipt_grace_days": 31},
    ],
)
def test_policy_ranges(fields: dict[str, Any]) -> None:
    thesis = ThesisFactory.create()
    _refused(lambda: TravelPolicy.objects.create(thesis=thesis, **fields))


# --------------------------------------------------------------------------- missions


def _mission(thesis: Any, **fields: Any) -> Any:
    from apps.travel.models import Mission

    values: dict[str, Any] = {
        "thesis": thesis,
        "start_date": D(2027, 2, 8),
        "end_date": D(2027, 2, 10),
        "destination": "LIRMM",
        "payer": "ST",
        "level": "INTERNAL_ST",
        "transports": ["TRAIN"],
        "lodging": "NONE",
        "status": "PLANNED",
    }
    values.update(fields)
    return Mission.objects.create(**values)


def test_a_valid_mission_row() -> None:
    _mission(ThesisFactory.create())


@pytest.mark.parametrize(
    "fields",
    [
        {"end_date": D(2027, 2, 7)},
        {"destination": "HOME"},
        {"payer": "UNIV"},
        {"status": "LOST"},
        {"lodging": "TENT"},
        {"level": "SECRET"},
        {"transports": []},
        {"transports": ["CAR"]},
        {"payer": "ST", "level": "INTERNAL_LIRMM"},
        {"payer": "LIRMM", "level": "INTERNAL_ST"},
    ],
)
def test_mission_constraints(fields: dict[str, Any]) -> None:
    thesis = ThesisFactory.create()
    _refused(lambda: _mission(thesis, **fields))


def test_an_edition_only_on_a_conference_mission() -> None:
    from apps.conferences.models import Edition, Venue

    thesis = ThesisFactory.create()
    venue = Venue.objects.create(thesis=thesis, kind="CONFERENCE", acronym="SFCA", name="Fictif")
    edition = Edition.objects.create(thesis=thesis, venue=venue, year=2027)
    _mission(
        thesis, destination="CONFERENCE", payer="LIRMM", level="INTERNAL_LIRMM", edition=edition
    )
    _refused(lambda: _mission(thesis, destination="LIRMM", edition=edition))


# --------------------------------------------------------------------------- orders


def _order(mission: Any, **fields: Any) -> Any:
    from apps.travel.models import MissionOrder

    values: dict[str, Any] = {
        "thesis": mission.thesis,
        "mission": mission,
        "requested_by": _user(),
        "payer": "ST",
        "snapshot_start": mission.start_date,
        "snapshot_end": mission.end_date,
        "snapshot_destination": mission.destination,
        "decision": "PENDING",
    }
    values.update(fields)
    return MissionOrder.objects.create(**values)


def _user() -> Any:
    from apps.accounts.tests.factories import UserFactory

    return UserFactory.create()


def test_one_pending_order_per_mission() -> None:
    mission = _mission(ThesisFactory.create())
    _order(mission)
    _refused(lambda: _order(mission))


@pytest.mark.parametrize(
    "fields",
    [
        {"decision": "MAYBE"},
        {"payer": "OTHER"},  # no order for another body
        {"decision": "GRANTED"},  # decided without decided_by / decided_at
        {"decision": "REFUSED", "reason": "", "decided_at": NOW_DT},
        {"decision": "GRANTED", "reason": "OTHER", "decided_at": NOW_DT},
        {"decision": "PENDING", "reason": "OTHER"},
        {"decision": "REFUSED", "reason": "TOO_EXPENSIVE", "decided_at": NOW_DT},
        {"decision": "WITHDRAWN"},  # without decided_at
        {"decision": "PENDING", "decided_at": NOW_DT},
    ],
)
def test_order_constraints(fields: dict[str, Any]) -> None:
    mission = _mission(ThesisFactory.create())
    if fields.get("decision") in ("GRANTED", "REFUSED") and "decided_at" in fields:
        fields = {**fields, "decided_by": _user()}
    _refused(lambda: _order(mission, **fields))


def test_an_order_is_never_decided_by_its_requester() -> None:
    mission = _mission(ThesisFactory.create())
    requester = _user()
    _refused(
        lambda: _order(
            mission,
            requested_by=requester,
            decided_by=requester,
            decision="GRANTED",
            decided_at=NOW_DT,
        )
    )
