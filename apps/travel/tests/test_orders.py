"""Mission orders (M7, step 4): requested by the student with a sealed estimate; decided
by the payer's person (tutor for ST, director for the LIRMM), who reads the mission,
with a recent second factor asked after the rights; refused with a reason from a
closed list; decided = history; an order covers the mission only while its snapshot
(dates, destination, payer) still matches."""

from __future__ import annotations

import datetime as dt
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection

from apps.accounts.models import Membership
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.travel import services
from apps.travel.models import HistoryError, Mission, MissionOrder
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


def _request(team: SimpleNamespace, mission: Mission, total: str = "420,00") -> MissionOrder:
    return services.request_order(mission=mission, user=team.student, estimated_total=total)


def _decide(
    team: SimpleNamespace, order: MissionOrder, role: str, decision: str = "GRANTED", **kw: Any
) -> MissionOrder:
    return services.decide_order(
        order=order, user=getattr(team, role), decision=decision, recent_mfa=True, **kw
    )


DECIDER = {"ST": "tutor", "LIRMM": "director"}


# =========================================================================== request


@pytest.mark.parametrize("payer", ["ST", "LIRMM"])
def test_the_student_requests_an_order(team: SimpleNamespace, payer: str) -> None:
    mission = mission_of(team, payer=payer)
    order = _request(team, mission)
    mission.refresh_from_db()
    assert mission.status == "ORDER_REQUESTED"
    assert (order.decision, order.payer) == ("PENDING", payer)
    assert (order.snapshot_start, order.snapshot_end, order.snapshot_destination) == (
        D(2027, 2, 8),
        D(2027, 2, 10),
        "LIRMM",
    )
    assert _events("MISSION_ORDER_REQUESTED") == [
        {
            "order_id": str(order.pk),
            "payer": payer,
            "start_date": "2027-02-08",
            "end_date": "2027-02-10",
        }
    ]


def test_the_estimate_is_sealed_and_canonical(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    _request(team, mission, total="1234,5")
    assert "1234.50" not in "\n".join(database_dump())
    (view,) = services.orders_of(team.student, mission)
    assert view.estimate == 123450


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor"])
def test_only_the_student_requests(team: SimpleNamespace, role: str) -> None:
    mission = mission_of(team)
    with pytest.raises(PermissionDenied):
        services.request_order(mission=mission, user=getattr(team, role), estimated_total="10")
    assert not MissionOrder.objects.exists()


def test_no_order_for_another_payer(team: SimpleNamespace) -> None:
    mission = mission_of(team, payer="OTHER")
    with pytest.raises(ValidationError) as info:
        _request(team, mission)
    assert info.value.code == "no_order"


@pytest.mark.parametrize("total", ["", "-5", "1e3", "\uff11\uff12"])
def test_an_invalid_estimate_is_refused(team: SimpleNamespace, total: str) -> None:
    with pytest.raises(ValidationError) as info:
        _request(team, mission_of(team), total=total)
    assert info.value.code == "amount"
    assert not MissionOrder.objects.exists()


def test_one_pending_order_per_mission(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    _request(team, mission)
    with pytest.raises(ValidationError) as info:
        _request(team, mission)
    assert info.value.code == "already_pending"


@pytest.mark.parametrize("payer", ["ST", "LIRMM"])
def test_a_request_needs_an_active_decider(team: SimpleNamespace, payer: str) -> None:
    decider = getattr(team, DECIDER[payer])
    Membership.objects.filter(thesis=team.thesis, user=decider).update(active_until=NOW)
    with pytest.raises(ValidationError) as info:
        _request(team, mission_of(team, payer=payer))
    assert info.value.code == "no_decider"


def test_a_refused_mission_can_be_requested_again(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    _decide(team, _request(team, mission), "tutor", "REFUSED", reason="MISSING_INFORMATION")
    second = _request(team, mission)
    assert second.decision == "PENDING"
    assert MissionOrder.objects.filter(mission=mission).count() == 2


# =========================================================================== decision


@pytest.mark.parametrize("payer", ["ST", "LIRMM"])
def test_the_payers_person_grants(team: SimpleNamespace, payer: str) -> None:
    mission = mission_of(team, payer=payer)
    order = _decide(team, _request(team, mission), DECIDER[payer], comment="Bon voyage fictif.")
    mission.refresh_from_db()
    assert (order.decision, order.decided_by_id) == ("GRANTED", getattr(team, DECIDER[payer]).pk)
    assert mission.status == "ORDER_GRANTED"
    assert services.order_covers(mission) is True
    assert _events("MISSION_ORDER_GRANTED") == [{"order_id": str(order.pk), "payer": payer}]


@pytest.mark.parametrize(
    ("payer", "role"),
    [
        ("ST", "director"),  # not ST's person
        ("LIRMM", "tutor"),  # not the LIRMM's person
        ("ST", "co_supervisor"),
        ("LIRMM", "co_supervisor"),
        ("ST", "student"),
        ("LIRMM", "csi"),
    ],
)
def test_nobody_else_decides(team: SimpleNamespace, payer: str, role: str) -> None:
    order = _request(team, mission_of(team, payer=payer))
    with pytest.raises(PermissionDenied):
        services.decide_order(
            order=order, user=getattr(team, role), decision="GRANTED", recent_mfa=False
        )
    order.refresh_from_db()
    assert order.decision == "PENDING"


def test_the_second_factor_is_asked_after_the_rights(team: SimpleNamespace) -> None:
    order = _request(team, mission_of(team))
    with pytest.raises(ReauthenticationRequiredError):
        services.decide_order(order=order, user=team.tutor, decision="GRANTED", recent_mfa=False)
    order.refresh_from_db()
    assert order.decision == "PENDING"


def test_a_refusal_needs_a_reason_from_the_list(team: SimpleNamespace) -> None:
    order = _request(team, mission_of(team))
    for reason in ("", "TOO_EXPENSIVE"):
        with pytest.raises(ValidationError) as info:
            _decide(team, order, "tutor", "REFUSED", reason=reason)
        assert info.value.code == "reason"
    refused = _decide(team, order, "tutor", "REFUSED", reason="BUDGET_EXHAUSTED")
    mission = Mission.objects.get(pk=order.mission_id)
    assert (refused.reason, mission.status) == ("BUDGET_EXHAUSTED", "ORDER_REFUSED")
    assert _events("MISSION_ORDER_REFUSED") == [
        {"order_id": str(order.pk), "payer": "ST", "reason": "BUDGET_EXHAUSTED"}
    ]


def test_a_grant_carries_no_reason(team: SimpleNamespace) -> None:
    order = _request(team, mission_of(team))
    with pytest.raises(ValidationError):
        _decide(team, order, "tutor", "GRANTED", reason="OTHER")


@pytest.mark.parametrize("decision", ["PENDING", "WITHDRAWN", "MAYBE"])
def test_only_grant_or_refuse(team: SimpleNamespace, decision: str) -> None:
    order = _request(team, mission_of(team))
    with pytest.raises(ValidationError):
        _decide(team, order, "tutor", decision)


def test_the_comment_is_sealed_never_journaled(team: SimpleNamespace) -> None:
    secret = marker()
    mission = mission_of(team)
    _decide(team, _request(team, mission), "tutor", "REFUSED", reason="OTHER", comment=secret)
    assert secret not in "\n".join(database_dump())
    (view,) = services.orders_of(team.student, mission)
    assert view.comment == secret


def test_the_decider_must_read_the_mission(team: SimpleNamespace) -> None:
    # A director who could not read the level could not decide: payer => level makes
    # it impossible through the services; the CHECK guards the table.
    mission = mission_of(team, payer="LIRMM", level=L.CONFIDENTIAL)
    order = _decide(team, _request(team, mission), "director")
    assert order.decision == "GRANTED"


def test_a_decided_order_is_history(team: SimpleNamespace) -> None:
    order = _decide(team, _request(team, mission_of(team)), "tutor")
    with pytest.raises(ValidationError) as info:
        _decide(team, order, "tutor", "REFUSED", reason="OTHER")
    assert info.value.code == "decided"
    order.decision = "REFUSED"
    with pytest.raises(HistoryError):
        order.save()
    with pytest.raises(HistoryError):
        order.delete()
    with pytest.raises(HistoryError):
        MissionOrder.objects.filter(pk=order.pk).update(decision="REFUSED")
    with pytest.raises(HistoryError):
        MissionOrder.objects.filter(pk=order.pk).delete()


@pytest.mark.serial
def test_two_simultaneous_decisions_decide_once(team: SimpleNamespace) -> None:
    order = _request(team, mission_of(team))
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def decide(decision: str) -> None:
        try:
            barrier.wait()
            kw = {"reason": "OTHER"} if decision == "REFUSED" else {}
            _decide(team, MissionOrder.objects.get(pk=order.pk), "tutor", decision, **kw)
            outcomes.append(decision)
        except ValidationError as exc:
            outcomes.append(str(exc.code))
        finally:
            connection.close()

    threads = [threading.Thread(target=decide, args=(d,)) for d in ("GRANTED", "REFUSED")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes).count("decided") == 1
    order.refresh_from_db()
    assert order.decision in ("GRANTED", "REFUSED")
    assert len(_events("MISSION_ORDER_GRANTED") + _events("MISSION_ORDER_REFUSED")) == 1


# =========================================================================== withdrawal


def test_the_student_withdraws_a_pending_order(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    order = services.withdraw_order(order=_request(team, mission), user=team.student)
    mission.refresh_from_db()
    assert (order.decision, mission.status) == ("WITHDRAWN", "PLANNED")
    assert order.decided_by_id is None and order.decided_at is not None
    with pytest.raises(ValidationError):
        services.withdraw_order(order=order, user=team.student)
    with pytest.raises(ValidationError):
        _decide(team, order, "tutor")


def test_cancelling_a_mission_withdraws_its_pending_order(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    order = _request(team, mission)
    services.cancel_mission(mission=mission, user=team.student)
    order.refresh_from_db()
    assert order.decision == "WITHDRAWN"


# =========================================================================== coverage


def test_dates_cannot_move_while_an_order_is_pending(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    _request(team, mission)
    for change in ({"end_date": D(2027, 2, 11)}, {"destination": "ROUSSET"}, {"payer": "LIRMM"}):
        with pytest.raises(ValidationError) as info:
            services.update_mission(mission=mission, user=team.student, changes=change)
        assert info.value.code == "order_pending"
    services.update_mission(mission=mission, user=team.student, changes={"purpose": "Précisé."})


def test_a_granted_order_stops_covering_changed_dates(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    order = _decide(team, _request(team, mission), "tutor")
    services.update_mission(mission=mission, user=team.student, changes={"purpose": "Précisé."})
    mission.refresh_from_db()
    assert mission.status == "ORDER_GRANTED" and services.order_covers(mission)
    services.update_mission(
        mission=mission, user=team.student, changes={"end_date": D(2027, 2, 11)}
    )
    mission.refresh_from_db()
    assert mission.status == "PLANNED"
    assert services.order_covers(mission) is False
    assert _events("MISSION_ORDER_INVALIDATED") == [
        {"order_id": str(order.pk), "fields": ["end_date"]}
    ]
    order.refresh_from_db()
    assert order.decision == "GRANTED"  # history: the grant itself is untouched


def test_done_with_a_covering_order(team: SimpleNamespace, time_machine: Any) -> None:
    mission = mission_of(team)
    _decide(team, _request(team, mission), "tutor")
    time_machine.move_to(dt.datetime(2027, 2, 10, 9, 0, tzinfo=dt.UTC), tick=False)
    services.mark_done(mission=mission, user=team.student)
    mission.refresh_from_db()
    assert mission.status == "DONE"


def test_no_done_after_an_invalidated_order(team: SimpleNamespace, time_machine: Any) -> None:
    mission = mission_of(team)
    _decide(team, _request(team, mission), "tutor")
    services.update_mission(
        mission=mission, user=team.student, changes={"end_date": D(2027, 2, 11)}
    )
    time_machine.move_to(dt.datetime(2027, 3, 1, 9, 0, tzinfo=dt.UTC), tick=False)
    with pytest.raises(ValidationError) as info:
        services.mark_done(mission=mission, user=team.student)
    assert info.value.code == "order_required"


# =========================================================================== reading


def test_order_texts_follow_the_mission_level(team: SimpleNamespace) -> None:
    mission = mission_of(team, payer="ST")  # INTERNAL_ST
    _decide(team, _request(team, mission, total="99"), "tutor", comment="Accord fictif.")
    (tutor_view,) = services.orders_of(team.tutor, mission)
    (director_view,) = services.orders_of(team.director, mission)
    assert (tutor_view.estimate, tutor_view.comment) == (9900, "Accord fictif.")
    assert director_view.estimate is vault.RESTRICTED
    assert director_view.comment is vault.RESTRICTED
    assert director_view.order.decision == "GRANTED"  # the decision itself is not sealed


def test_the_deciders_queue(team: SimpleNamespace) -> None:
    st = _request(team, mission_of(team, payer="ST"))
    lab = _request(team, mission_of(team, payer="LIRMM"))
    assert [o.pk for o in services.pending_orders_for(team.tutor, team.thesis)] == [st.pk]
    assert [o.pk for o in services.pending_orders_for(team.director, team.thesis)] == [lab.pk]
    assert services.pending_orders_for(team.co_supervisor, team.thesis) == []
    assert services.pending_orders_for(team.student, team.thesis) == []


def test_level_change_reseals_order_texts(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    _decide(team, _request(team, mission, total="10"), "tutor", comment="Accord fictif.")
    services.change_mission_level(
        mission=mission, user=team.student, level=L.CONFIDENTIAL, recent_mfa=True
    )
    (view,) = services.orders_of(team.director, mission)  # CONFIDENTIAL: director reads
    assert (view.estimate, view.comment) == (1000, "Accord fictif.")
