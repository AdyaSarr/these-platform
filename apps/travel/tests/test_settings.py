"""Travel settings (M7): the target and the counting rules are set by the director or
the tutor, the reminder periods by the student; each asks for a recent second factor
(checked after the rights) and is journaled from -> to."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.travel import services
from apps.travel.models import TimeSplitTarget, TravelPolicy
from apps.travel.tests.helpers import NOW, make_team
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)

SUPERVISORS = ("director", "tutor")
OTHERS = ("student", "co_supervisor", "csi", "guest", "outsider")


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=False)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _events(action: str) -> list[dict[str, Any]]:
    return [e.details_json for e in AuditEvent.objects.filter(action=action).order_by("seq")]


def test_defaults_without_a_policy_row(team: SimpleNamespace) -> None:
    policy = services.policy_of(team.thesis)
    assert policy.pk is None or not TravelPolicy.objects.filter(pk=policy.pk).exists()
    assert (policy.remote_counts_as, policy.conference_counts_as) == ("ST", "LAB")
    assert (policy.order_lead_days, policy.receipt_grace_days) == (15, 3)


# =========================================================================== rules


@pytest.mark.parametrize("role", SUPERVISORS)
def test_supervisors_set_the_rules(team: SimpleNamespace, role: str) -> None:
    services.set_rules(
        thesis=team.thesis,
        user=getattr(team, role),
        recent_mfa=True,
        gap_alert_points=15,
        min_lab_days_per_month=4,
    )
    policy = TravelPolicy.objects.get(thesis=team.thesis)
    assert (policy.gap_alert_points, policy.min_lab_days_per_month) == (15, 4)
    assert _events("TRAVEL_RULES_CHANGED") == [
        {
            "changes": {
                "gap_alert_points": {"from": 10, "to": 15},
                "min_lab_days_per_month": {"from": None, "to": 4},
            }
        }
    ]


@pytest.mark.parametrize("role", OTHERS)
def test_others_never_set_the_rules(team: SimpleNamespace, role: str) -> None:
    # Refused on the rights, before any second factor is asked for.
    with pytest.raises(PermissionDenied):
        services.set_rules(
            thesis=team.thesis, user=getattr(team, role), recent_mfa=False, gap_alert_points=15
        )
    assert not TravelPolicy.objects.exists()


def test_rules_ask_for_a_recent_second_factor(team: SimpleNamespace) -> None:
    with pytest.raises(ReauthenticationRequiredError):
        services.set_rules(
            thesis=team.thesis, user=team.director, recent_mfa=False, gap_alert_points=15
        )
    assert not TravelPolicy.objects.exists()
    assert _events("TRAVEL_RULES_CHANGED") == []


def test_unchanged_rules_are_not_journaled(team: SimpleNamespace) -> None:
    services.set_rules(thesis=team.thesis, user=team.tutor, recent_mfa=True, gap_alert_points=10)
    assert _events("TRAVEL_RULES_CHANGED") == []


@pytest.mark.parametrize(
    "fields",
    [
        {"remote_counts_as": "HOME"},
        {"conference_counts_as": ""},
        {"whit_monday_holiday": "yes"},
        {"gap_alert_points": 0},
        {"gap_alert_points": 51},
        {"gap_alert_points": 10.0},
        {"gap_alert_points": True},
        {"min_lab_days_per_month": -1},
        {"min_lab_days_per_month": 24},
    ],
)
def test_invalid_rules(team: SimpleNamespace, fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        services.set_rules(thesis=team.thesis, user=team.director, recent_mfa=True, **fields)


def test_rules_cannot_carry_reminder_periods(team: SimpleNamespace) -> None:
    with pytest.raises(TypeError):
        services.set_rules(
            thesis=team.thesis, user=team.director, recent_mfa=True, order_lead_days=5
        )  # type: ignore[call-arg]


# =========================================================================== target


@pytest.mark.parametrize("role", SUPERVISORS)
def test_supervisors_set_the_target(team: SimpleNamespace, role: str) -> None:
    user = getattr(team, role)
    services.set_target(thesis=team.thesis, user=user, recent_mfa=True, cifre_year=1, st_percent=60)
    services.set_target(thesis=team.thesis, user=user, recent_mfa=True, cifre_year=1, st_percent=55)
    services.set_target(thesis=team.thesis, user=user, recent_mfa=True, cifre_year=1, st_percent=55)
    target = TimeSplitTarget.objects.get(thesis=team.thesis, cifre_year=1)
    assert (target.st_percent, target.lab_percent) == (55, 45)
    assert _events("TRAVEL_TARGET_CHANGED") == [
        {"cifre_year": 1, "from": None, "to": 60},
        {"cifre_year": 1, "from": 60, "to": 55},
    ]


@pytest.mark.parametrize("role", OTHERS)
def test_others_never_set_the_target(team: SimpleNamespace, role: str) -> None:
    with pytest.raises(PermissionDenied):
        services.set_target(
            thesis=team.thesis,
            user=getattr(team, role),
            recent_mfa=False,
            cifre_year=1,
            st_percent=60,
        )
    assert not TimeSplitTarget.objects.exists()


def test_target_asks_for_a_recent_second_factor(team: SimpleNamespace) -> None:
    with pytest.raises(ReauthenticationRequiredError):
        services.set_target(
            thesis=team.thesis, user=team.tutor, recent_mfa=False, cifre_year=1, st_percent=60
        )
    assert not TimeSplitTarget.objects.exists()


@pytest.mark.parametrize(
    ("cifre_year", "st_percent"),
    [(0, 50), (4, 50), (1, -1), (1, 101), (1, 50.0), (1, True)],
)
def test_invalid_target(team: SimpleNamespace, cifre_year: Any, st_percent: Any) -> None:
    with pytest.raises(ValidationError):
        services.set_target(
            thesis=team.thesis,
            user=team.director,
            recent_mfa=True,
            cifre_year=cifre_year,
            st_percent=st_percent,
        )


# =========================================================================== reminders


def test_the_student_sets_the_reminder_periods(team: SimpleNamespace) -> None:
    services.set_reminders(
        thesis=team.thesis,
        user=team.student,
        recent_mfa=True,
        order_lead_days=21,
        receipt_grace_days=3,
    )
    policy = TravelPolicy.objects.get(thesis=team.thesis)
    assert (policy.order_lead_days, policy.receipt_grace_days) == (21, 3)
    assert _events("TRAVEL_REMINDERS_CHANGED") == [
        {"changes": {"order_lead_days": {"from": 15, "to": 21}}}
    ]


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor", "csi", "guest", "outsider"])
def test_others_never_set_the_reminder_periods(team: SimpleNamespace, role: str) -> None:
    with pytest.raises(PermissionDenied):
        services.set_reminders(
            thesis=team.thesis,
            user=getattr(team, role),
            recent_mfa=False,
            order_lead_days=21,
            receipt_grace_days=3,
        )


def test_reminders_ask_for_a_recent_second_factor(team: SimpleNamespace) -> None:
    with pytest.raises(ReauthenticationRequiredError):
        services.set_reminders(
            thesis=team.thesis,
            user=team.student,
            recent_mfa=False,
            order_lead_days=21,
            receipt_grace_days=3,
        )
    assert not TravelPolicy.objects.exists()


@pytest.mark.parametrize(("lead", "grace"), [(-1, 3), (91, 3), (15, -1), (15, 31), (15.0, 3)])
def test_invalid_reminder_periods(team: SimpleNamespace, lead: Any, grace: Any) -> None:
    with pytest.raises(ValidationError):
        services.set_reminders(
            thesis=team.thesis,
            user=team.student,
            recent_mfa=True,
            order_lead_days=lead,
            receipt_grace_days=grace,
        )


def test_rules_and_reminders_share_one_policy_row(team: SimpleNamespace) -> None:
    services.set_rules(thesis=team.thesis, user=team.tutor, recent_mfa=True, gap_alert_points=20)
    services.set_reminders(
        thesis=team.thesis,
        user=team.student,
        recent_mfa=True,
        order_lead_days=30,
        receipt_grace_days=7,
    )
    policy = TravelPolicy.objects.get(thesis=team.thesis)
    assert (policy.gap_alert_points, policy.order_lead_days, policy.receipt_grace_days) == (
        20,
        30,
        7,
    )
