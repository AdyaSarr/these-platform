"""Database constraints of the travel module (M7): each CHECK and UNIQUE is hit in
SQL, bypassing the services."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from django.db import IntegrityError, transaction

from apps.accounts.tests.factories import ThesisFactory
from apps.travel.models import PresenceDay, TimeSplitTarget, TravelPolicy
from apps.travel.tests.helpers import D

pytestmark = pytest.mark.django_db


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
        {"remote_counts_as": "HOME"},
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
