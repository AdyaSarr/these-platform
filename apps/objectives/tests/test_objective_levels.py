"""An objective is PUBLIC, INTERNAL_LIRMM or CONFIDENTIAL, never INTERNAL_ST
(M4): refused by the service and by the database (replaces the case skipped by
apps/activity/tests/test_reads.py, M11b)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.core.confidentiality import ConfidentialityLevel as L
from apps.objectives.models import Objective
from apps.objectives.tests.helpers import make_team, objective_of

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def test_the_service_refuses_internal_st(team: SimpleNamespace) -> None:
    with pytest.raises(ValidationError):
        objective_of(team, level=L.INTERNAL_ST)
    assert not Objective.objects.exists()


def test_the_database_refuses_internal_st(team: SimpleNamespace) -> None:
    goal = objective_of(team, level=L.PUBLIC)
    with pytest.raises(IntegrityError, match="objectives_objective_level"), transaction.atomic():
        Objective.objects.filter(pk=goal.pk).update(level=L.INTERNAL_ST)
