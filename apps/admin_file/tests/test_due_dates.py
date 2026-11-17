"""Due dates (M3, revised 2026-11-14), at service level.

- Catalogue item: only a validator role of the item changes it (the view asks a
  recent second factor); a mandatory one never loses it; the student notified.
- Custom item: the student, as before.
- The student PROPOSES a date for a catalogue item, with a reason from a closed
  list; a validator accepts or refuses; one pending proposal per item.
- Every change is audited with ``from`` / ``to`` (ISO dates), never the comment.
- Dates between the CIFRE start and the planned end of the thesis.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from apps.accounts.models import Membership, Thesis
from apps.accounts.roles import Role
from apps.admin_file import services
from apps.admin_file.models import (
    DueDateProposal,
    DueDateReason,
    ItemNotification,
    RequiredItem,
    Section,
)
from apps.admin_file.tests.helpers import item_of, make_team
from apps.admin_file.transitions import FrozenFileError
from apps.core.confidentiality import ConfidentialityLevel
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db

D, T = Role.DIRECTOR, Role.INDUSTRIAL_TUTOR
P = DueDateProposal.Status
DAY = dt.date(2027, 1, 15)  # inside the thesis (ThesisFactory: 2026-03-01 .. 2029-02-28)
LATER = dt.date(2027, 3, 1)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _cifre(team: SimpleNamespace) -> RequiredItem:
    return item_of(team, section=Section.CIFRE, validator_roles=[T], due_date=DAY)


def _academic(team: SimpleNamespace) -> RequiredItem:
    return item_of(team, section=Section.ACADEMIC, validator_roles=[D], due_date=DAY)


def _two_party(team: SimpleNamespace) -> RequiredItem:
    return item_of(team, section=Section.CIFRE, validator_roles=[D, T], due_date=DAY)


def _custom(team: SimpleNamespace) -> RequiredItem:
    return services.create_custom_item(
        thesis=team.thesis,
        user=team.student,
        label="Pièce personnalisée fictive",
        section=Section.ACADEMIC,
        validator_roles=[D],
        default_level=ConfidentialityLevel.CONFIDENTIAL,
        mandatory=True,
        attachment_required=False,
        due_date=DAY,
    )


def _last(action: str) -> AuditEvent:
    return AuditEvent.objects.filter(action=action).order_by("-seq").first()  # type: ignore[return-value]


def _due(item: RequiredItem) -> dt.date | None:
    return RequiredItem.objects.get(pk=item.pk).due_date


# --------------------------------------------------------------------------- direct change


@pytest.mark.parametrize(
    ("kind", "allowed"),
    [
        ("cifre", {"tutor"}),
        ("academic", {"director"}),
        ("two_party", {"director", "tutor"}),
    ],
)
def test_only_a_validator_of_a_catalogue_item_changes_it(
    team: SimpleNamespace, kind: str, allowed: set[str]
) -> None:
    for role in ("student", "director", "co_supervisor", "tutor", "csi", "guest"):
        item = {"cifre": _cifre, "academic": _academic, "two_party": _two_party}[kind](team)
        user = getattr(team, role)
        if role in allowed:
            services.update_due_date(item=item, user=user, due_date=LATER)
            assert _due(item) == LATER
        else:
            with pytest.raises(PermissionDenied):
                services.update_due_date(item=item, user=user, due_date=LATER)
            assert _due(item) == DAY


def test_a_refusal_is_audited(team: SimpleNamespace) -> None:
    item = _cifre(team)
    with pytest.raises(PermissionDenied):
        services.update_due_date(item=item, user=team.director, due_date=LATER)
    event = _last("ADMIN_ITEM_ACCESS_DENIED")
    assert event.actor == team.director
    assert event.details_json["reason"] == "not_validator"


def test_the_change_is_audited_from_and_to(team: SimpleNamespace) -> None:
    item = _academic(team)
    services.update_due_date(item=item, user=team.director, due_date=LATER)
    event = _last("ADMIN_ITEM_UPDATED")
    assert event.details_json == {"field": "due_date", "from": "2027-01-15", "to": "2027-03-01"}


def test_a_mandatory_catalogue_item_keeps_a_due_date(team: SimpleNamespace) -> None:
    item = _academic(team)
    with pytest.raises(ValidationError) as caught:
        services.update_due_date(item=item, user=team.director, due_date=None)
    assert caught.value.code == "due_date_required"
    assert _due(item) == DAY


def test_the_database_refuses_it_too(team: SimpleNamespace) -> None:
    item = _academic(team)
    with pytest.raises(IntegrityError), transaction.atomic():
        RequiredItem.objects.filter(pk=item.pk).update(due_date=None)


def test_an_optional_catalogue_item_may_lose_it(team: SimpleNamespace) -> None:
    item = item_of(team, validator_roles=[D], due_date=DAY, mandatory=False)
    services.update_due_date(item=item, user=team.director, due_date=None)
    assert _due(item) is None
    assert _last("ADMIN_ITEM_UPDATED").details_json["to"] is None


@pytest.mark.parametrize(
    ("day", "code"),
    [(dt.date(2026, 2, 28), "due_date_before_start"), (dt.date(2029, 3, 1), "due_date_after_end")],
)
def test_dates_stay_inside_the_thesis(team: SimpleNamespace, day: dt.date, code: str) -> None:
    item = _academic(team)
    with pytest.raises(ValidationError) as caught:
        services.update_due_date(item=item, user=team.director, due_date=day)
    assert caught.value.code == code
    with pytest.raises(ValidationError) as caught:
        services.propose_due_date(
            item=item, user=team.student, due_date=day, reason=DueDateReason.OTHER
        )
    assert caught.value.code == code


def test_the_bounds_themselves_are_allowed(team: SimpleNamespace) -> None:
    item = _academic(team)
    for day in (team.thesis.cifre_start_date, team.thesis.planned_end_date):
        services.update_due_date(item=item, user=team.director, due_date=day)
        assert _due(item) == day


def test_a_direct_change_notifies_the_student(
    team: SimpleNamespace, django_capture_on_commit_callbacks: Any
) -> None:
    item = _cifre(team)
    with django_capture_on_commit_callbacks(execute=True):
        services.update_due_date(item=item, user=team.tutor, due_date=LATER)
    note = ItemNotification.objects.get()
    assert (note.kind, note.recipient) == ("DUE_CHANGED", team.student)
    assert [m.to for m in mail.outbox] == [[team.student.email]]


def test_frozen_thesis(team: SimpleNamespace) -> None:
    item = _academic(team)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.DEFENDED)
    with pytest.raises(FrozenFileError):
        services.update_due_date(item=item, user=team.director, due_date=LATER)
    with pytest.raises(FrozenFileError):
        services.propose_due_date(
            item=item, user=team.student, due_date=LATER, reason=DueDateReason.OTHER
        )


# --------------------------------------------------------------------------- custom items


def test_the_student_changes_a_custom_item_as_before(team: SimpleNamespace) -> None:
    item = _custom(team)
    services.update_due_date(item=item, user=team.student, due_date=LATER)
    assert _due(item) == LATER
    services.update_due_date(item=item, user=team.student, due_date=None)  # may be cleared
    assert _due(item) is None
    with pytest.raises(PermissionDenied):
        services.update_due_date(item=item, user=team.director, due_date=DAY)
    assert not ItemNotification.objects.exists()


def test_no_proposal_for_a_custom_item(team: SimpleNamespace) -> None:
    with pytest.raises(ValidationError) as caught:
        services.propose_due_date(
            item=_custom(team), user=team.student, due_date=LATER, reason=DueDateReason.OTHER
        )
    assert caught.value.code == "custom_item"


# --------------------------------------------------------------------------- proposals


def _propose(team: SimpleNamespace, item: RequiredItem, **kwargs: Any) -> DueDateProposal:
    kwargs.setdefault("due_date", LATER)
    kwargs.setdefault("reason", DueDateReason.WAITING_THIRD_PARTY)
    return services.propose_due_date(item=item, user=team.student, **kwargs)


def test_the_student_proposes(
    team: SimpleNamespace, django_capture_on_commit_callbacks: Any
) -> None:
    item = _two_party(team)
    with django_capture_on_commit_callbacks(execute=True):
        proposal = _propose(team, item, comment="Commentaire fictif")
    assert (proposal.status, proposal.from_date, proposal.to_date) == (P.PENDING, DAY, LATER)
    assert _due(item) == DAY  # nothing changes before a decision
    event = _last("ADMIN_DUE_DATE_PROPOSED")
    assert event.details_json == {
        "from": "2027-01-15",
        "to": "2027-03-01",
        "proposal": str(proposal.pk),
        "reason": "WAITING_THIRD_PARTY",
    }
    assert sorted(m.to[0] for m in mail.outbox) == sorted([team.director.email, team.tutor.email])


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor", "csi", "guest"])
def test_only_the_student_proposes(team: SimpleNamespace, role: str) -> None:
    with pytest.raises(PermissionDenied):
        services.propose_due_date(
            item=_cifre(team),
            user=getattr(team, role),
            due_date=LATER,
            reason=DueDateReason.OTHER,
        )
    assert not DueDateProposal.objects.exists()


def test_one_pending_proposal_per_item(team: SimpleNamespace) -> None:
    item = _cifre(team)
    _propose(team, item)
    with pytest.raises(ValidationError) as caught:
        _propose(team, item, due_date=dt.date(2027, 4, 1))
    assert caught.value.code == "pending_proposal"
    assert DueDateProposal.objects.filter(item=item).count() == 1


def test_the_database_allows_one_pending_proposal_only(team: SimpleNamespace) -> None:
    item = _cifre(team)
    first = _propose(team, item)
    with pytest.raises(IntegrityError), transaction.atomic():
        DueDateProposal.objects.create(
            item=item,
            proposed_by=team.student,
            from_date=DAY,
            to_date=LATER,
            reason_code=DueDateReason.OTHER,
        )
    assert first.status == P.PENDING


@pytest.mark.parametrize("reason", ["", "WRONG", "AMENDMENT ", None])
def test_the_reason_comes_from_the_closed_list(team: SimpleNamespace, reason: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        _propose(team, _cifre(team), reason=reason)
    assert caught.value.code == "reason"


def test_a_proposal_needs_a_date(team: SimpleNamespace) -> None:
    with pytest.raises(ValidationError) as caught:
        _propose(team, _cifre(team), due_date=None)
    assert caught.value.code == "due_date_required"


@pytest.mark.parametrize(
    ("roles", "message"),
    [
        ([T], "Aucun·e tuteur·rice industriel·le dans l'équipe\u00a0: invitez-le·la d'abord."),
        ([D], "Aucun·e directeur·rice de thèse dans l'équipe\u00a0: invitez-le·la d'abord."),
    ],
)
def test_no_proposal_without_an_active_validator(
    team: SimpleNamespace, roles: list[Role], message: str
) -> None:
    from django.utils import timezone
    from django.utils.translation import override

    section = Section.CIFRE if T in roles else Section.ACADEMIC
    item = item_of(team, section=section, validator_roles=roles, due_date=DAY)
    Membership.objects.filter(thesis=team.thesis, role__in=roles).update(
        active_until=timezone.now()
    )
    with override("fr"), pytest.raises(ValidationError) as caught:
        _propose(team, item)
    assert caught.value.code == "no_validator"
    assert caught.value.messages == [message]
    assert not DueDateProposal.objects.exists()


def test_one_of_two_validators_is_enough_to_propose(team: SimpleNamespace) -> None:
    from django.utils import timezone

    item = _two_party(team)
    Membership.objects.filter(user=team.tutor).update(active_until=timezone.now())
    assert _propose(team, item).status == P.PENDING


# --------------------------------------------------------------------------- decisions


def test_accept(team: SimpleNamespace, django_capture_on_commit_callbacks: Any) -> None:
    item = _cifre(team)
    proposal = _propose(team, item)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        services.accept_due_date(proposal=proposal, user=team.tutor)
    proposal.refresh_from_db()
    assert (proposal.status, proposal.decided_by) == (P.ACCEPTED, team.tutor)
    assert proposal.decided_at is not None
    assert _due(item) == LATER
    event = _last("ADMIN_DUE_DATE_ACCEPTED")
    assert event.details_json == {
        "from": "2027-01-15",
        "to": "2027-03-01",
        "proposal": str(proposal.pk),
    }
    assert [m.to for m in mail.outbox] == [[team.student.email]]
    assert ItemNotification.objects.filter(kind="DUE_ACCEPTED").get().recipient == team.student


def test_refuse(team: SimpleNamespace, django_capture_on_commit_callbacks: Any) -> None:
    item = _cifre(team)
    proposal = _propose(team, item)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        services.refuse_due_date(proposal=proposal, user=team.tutor)
    proposal.refresh_from_db()
    assert proposal.status == P.REFUSED
    assert _due(item) == DAY
    assert _last("ADMIN_DUE_DATE_REFUSED").details_json["to"] == "2027-03-01"
    assert [m.to for m in mail.outbox] == [[team.student.email]]


@pytest.mark.parametrize("role", ["student", "director", "co_supervisor", "csi", "guest"])
@pytest.mark.parametrize("decision", ["accept_due_date", "refuse_due_date"])
def test_only_a_validator_decides(team: SimpleNamespace, role: str, decision: str) -> None:
    proposal = _propose(team, _cifre(team))
    with pytest.raises(PermissionDenied):
        getattr(services, decision)(proposal=proposal, user=getattr(team, role))
    proposal.refresh_from_db()
    assert proposal.status == P.PENDING


def test_a_decided_proposal_is_not_decided_again(team: SimpleNamespace) -> None:
    proposal = _propose(team, _cifre(team))
    services.refuse_due_date(proposal=proposal, user=team.tutor)
    with pytest.raises(ValidationError) as caught:
        services.accept_due_date(proposal=proposal, user=team.tutor)
    assert caught.value.code == "not_pending"


def test_a_direct_change_supersedes_the_pending_proposal(team: SimpleNamespace) -> None:
    item = _cifre(team)
    proposal = _propose(team, item)
    services.update_due_date(item=item, user=team.tutor, due_date=dt.date(2027, 2, 1))
    proposal.refresh_from_db()
    assert (proposal.status, proposal.decided_by) == (P.SUPERSEDED, team.tutor)
    event = _last("ADMIN_DUE_DATE_SUPERSEDED")
    assert event.details_json["proposal"] == str(proposal.pk)
    with pytest.raises(ValidationError):
        services.accept_due_date(proposal=proposal, user=team.tutor)
    assert _due(item) == dt.date(2027, 2, 1)


def test_the_student_withdraws(team: SimpleNamespace) -> None:
    item = _cifre(team)
    proposal = _propose(team, item)
    with pytest.raises(PermissionDenied):
        services.withdraw_due_date_proposal(proposal=proposal, user=team.tutor)
    services.withdraw_due_date_proposal(proposal=proposal, user=team.student)
    proposal.refresh_from_db()
    assert proposal.status == P.WITHDRAWN
    assert _last("ADMIN_DUE_DATE_WITHDRAWN").details_json["proposal"] == str(proposal.pk)
    assert _propose(team, item).status == P.PENDING  # a new one is possible


def test_the_comment_never_reaches_the_log(team: SimpleNamespace) -> None:
    proposal = _propose(team, _cifre(team), comment="Commentaire fictif QWERTY")
    services.accept_due_date(proposal=proposal, user=team.tutor)
    assert "QWERTY" not in " ".join(str(e.details_json) for e in AuditEvent.objects.all())
    assert "QWERTY" not in str(proposal)


def test_a_proposal_of_another_item_is_refused(team: SimpleNamespace) -> None:
    first, second = _cifre(team), _cifre(team)
    proposal = _propose(team, first)
    with pytest.raises(PermissionDenied):
        services.proposal_for(team.tutor, second, proposal.pk, operation="accept_due_date")
    assert services.proposal_for(team.tutor, first, proposal.pk, operation="x") == proposal


# ------------------------------------------------------------- one control for every date


@pytest.mark.parametrize(
    ("day", "code"),
    [(dt.date(2026, 2, 28), "due_date_before_start"), (dt.date(2029, 3, 1), "due_date_after_end")],
)
def test_a_custom_item_is_created_inside_the_thesis_only(
    team: SimpleNamespace, day: dt.date, code: str
) -> None:
    events = AuditEvent.objects.count()
    with pytest.raises(ValidationError) as caught:
        services.create_custom_item(
            thesis=team.thesis,
            user=team.student,
            label="Pièce hors bornes fictive",
            section=Section.ACADEMIC,
            validator_roles=[D],
            default_level=ConfidentialityLevel.CONFIDENTIAL,
            mandatory=True,
            attachment_required=False,
            due_date=day,
        )
    assert caught.value.code == code
    assert not RequiredItem.objects.filter(label="Pièce hors bornes fictive").exists()
    assert AuditEvent.objects.count() == events


def test_a_custom_item_may_still_be_created_without_a_due_date(team: SimpleNamespace) -> None:
    item = services.create_custom_item(
        thesis=team.thesis,
        user=team.student,
        label="Pièce sans échéance fictive",
        section=Section.ACADEMIC,
        validator_roles=[D],
        default_level=ConfidentialityLevel.CONFIDENTIAL,
        mandatory=True,
        attachment_required=False,
        due_date=None,
    )
    assert item.due_date is None


def test_every_due_date_entry_goes_through_one_control() -> None:
    """Every service receiving a due date calls _check_bounds, and no other code of
    the module compares a date with the thesis bounds."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(services))
    entries = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name == "_check_bounds":
            continue
        params = {a.arg for a in [*node.args.args, *node.args.kwonlyargs]}
        if "due_date" in params:
            entries.append(node.name)
            calls = {
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            assert "_check_bounds" in calls, node.name
        for attr in ast.walk(node):
            if isinstance(attr, ast.Attribute) and attr.attr in {
                "cifre_start_date",
                "planned_end_date",
            }:
                raise AssertionError(f"{node.name} compares with the thesis bounds itself")
    assert {"create_custom_item", "update_due_date", "propose_due_date"} <= set(entries)
