"""Test helpers: a fictitious team and items put in a given state (valid rows)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from django.utils import timezone

from apps.accounts.models import Thesis
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory
from apps.admin_file.models import ItemStatus, RequiredItem
from apps.admin_file.tests.factories import RequiredItemFactory


def make_team(*, status: str = Thesis.Status.SETUP, mfa: bool = False) -> SimpleNamespace:
    thesis = ThesisFactory.create(status=status)

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(thesis=thesis, role=role, user__mfa=mfa, **kwargs).user

    return SimpleNamespace(
        thesis=thesis,
        student=member(Role.PHD_STUDENT),
        director=member(Role.DIRECTOR),
        co_supervisor=member(Role.CO_SUPERVISOR),
        tutor=member(Role.INDUSTRIAL_TUTOR),
        csi=member(Role.CSI_MEMBER, active_until=timezone.now() + dt.timedelta(days=60)),
        guest=member(Role.GUEST),
    )


def put(item: RequiredItem, status: str, submitter: Any) -> RequiredItem:
    """Write ``item`` in ``status`` with consistent fields (no service, no audit)."""
    fields: dict[str, Any] = {
        "status": status,
        "validated_at": timezone.now() if status == ItemStatus.VALIDATED else None,
        "reason_code": "ILLEGIBLE" if status == ItemStatus.REJECTED else "",
        "cycle": 0 if status == ItemStatus.TODO else 1,
        "submitted_by": None if status == ItemStatus.TODO else submitter,
    }
    RequiredItem.objects.filter(pk=item.pk).update(**fields)
    item.refresh_from_db()
    return item


def item_of(team: SimpleNamespace, **kwargs: Any) -> RequiredItem:
    """An item WITHOUT required attachment (no vault involved), director-validated."""
    kwargs.setdefault("attachment_required", False)
    return RequiredItemFactory.create(thesis=team.thesis, **kwargs)
