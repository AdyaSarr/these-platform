"""RequiredItem.submitted_at (frontend step 2, point 12): the date of the current
deposit, only while the item is deposited.

The database refuses a deposit date on an item that is not SUBMITTED (CHECK
admin_file_item_submitted_at_status); the other way round stays free: an item
deposited before the field existed is SUBMITTED with no date.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.admin_file import services
from apps.admin_file.models import ItemStatus, RequiredItem
from apps.admin_file.tests.helpers import item_of, make_team, put

pytestmark = pytest.mark.django_db(transaction=True)

MONDAY = dt.datetime(2027, 3, 1, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


@pytest.mark.parametrize("status", [ItemStatus.TODO, ItemStatus.VALIDATED, ItemStatus.REJECTED])
def test_the_database_refuses_a_deposit_date_off_a_deposit(
    team: SimpleNamespace, status: str
) -> None:
    item = put(item_of(team), status, team.student)
    with pytest.raises(IntegrityError), transaction.atomic():
        RequiredItem.objects.filter(pk=item.pk).update(submitted_at=timezone.now())


def test_a_deposit_may_have_no_date(team: SimpleNamespace) -> None:
    item = put(item_of(team), ItemStatus.SUBMITTED, team.student)  # an older deposit
    assert item.submitted_at is None
    RequiredItem.objects.filter(pk=item.pk).update(submitted_at=timezone.now())


def test_the_services_keep_the_rule(team: SimpleNamespace, time_machine: Any) -> None:
    time_machine.move_to(MONDAY, tick=False)
    item = item_of(team)  # validated by the director alone
    services.submit(item=item, user=team.student)
    item.refresh_from_db()
    assert (item.status, item.submitted_at) == (ItemStatus.SUBMITTED, timezone.now())
    services.approve(item=item, user=team.director)
    item.refresh_from_db()
    assert (item.status, item.submitted_at) == (ItemStatus.VALIDATED, None)
