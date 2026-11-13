"""A refusal born from an IntegrityError (one approval per person and cycle) is
journalled and the connection stays usable, as in production: transaction=True,
so no enclosing test transaction hides an aborted PostgreSQL transaction.

The IntegrityError is confined to a savepoint (nested atomic around the
ItemApproval insert); the work transaction is then rolled back by _DeniedError,
and ADMIN_ITEM_ACCESS_DENIED is written in a fresh transaction afterwards.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory
from apps.admin_file import services
from apps.admin_file.models import ItemApproval, ItemStatus, RequiredItem, Section
from apps.admin_file.tests.helpers import item_of, make_team
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)


def test_already_approved_is_committed_to_the_log_and_the_connection_survives() -> None:
    team = make_team()
    both = MembershipFactory.create(thesis=team.thesis, role=Role.DIRECTOR, user__mfa=False).user
    MembershipFactory.create(thesis=team.thesis, role=Role.INDUSTRIAL_TUTOR, user=both)
    item = item_of(
        team,
        code="collaboration_contract",
        section=Section.CIFRE,
        validator_roles=[Role.DIRECTOR, Role.INDUSTRIAL_TUTOR],
    )
    services.submit(item=item, user=team.student)
    services.approve(item=item, user=both)  # committed: role DIRECTOR

    with pytest.raises(PermissionDenied):
        services.approve(item=item, user=both)  # role INDUSTRIAL_TUTOR -> UNIQUE(user)

    assert not connection.in_atomic_block  # nothing left open by the refusal
    assert not connection.needs_rollback
    event = (
        AuditEvent.objects.filter(action="ADMIN_ITEM_ACCESS_DENIED", actor=both)
        .order_by("-seq")
        .get()
    )
    assert event.details_json == {"operation": "approve", "reason": "already_approved"}
    assert event.object_id == str(item.pk)
    # The first approval (earlier transaction) is intact; nothing of the refused one.
    assert list(ItemApproval.objects.filter(item=item).values_list("role", "user")) == [
        (Role.DIRECTOR, both.pk)
    ]
    assert RequiredItem.objects.get(pk=item.pk).status == ItemStatus.SUBMITTED
    # And the same connection keeps working: the other party validates.
    assert services.approve(item=item, user=team.tutor).status == ItemStatus.VALIDATED
