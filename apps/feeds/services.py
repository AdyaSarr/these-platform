"""Personal iCal feeds (M12): creation, regeneration, revocation, lookup, daily run.

The token is a bearer secret, shown once: only its fingerprint is stored. Creating
and regenerating need a recent second factor (rule 6); revoking does not (it
reduces access). Every change is journaled with a reason code, never the token.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.accounts.models import Membership, Thesis
from apps.accounts.permissions import roles_of
from apps.accounts.roles import Role
from apps.accounts.statuses import open_statuses
from apps.vault.crypto import hash_token, new_token, tokens_match

from .models import CalendarFeed

ALLOWED_ROLES = frozenset(
    {
        Role.PHD_STUDENT,
        Role.DIRECTOR,
        Role.CO_SUPERVISOR,
        Role.INDUSTRIAL_TUTOR,
        Role.INDUSTRIAL_CO_SUPERVISOR,
    }
)
LIFETIME_MONTHS = 12
NOTICE_DAYS = 14
OPEN = open_statuses("feeds")  # apps/accounts/statuses.py


def allowed(user: Any, thesis: Thesis) -> bool:
    """A member of an open thesis, holding a role that may have a feed (never the
    CSI member nor a guest)."""
    # The status is read in the database, never on a possibly stale object (M13a).
    is_open = Thesis.objects.filter(pk=thesis.pk, status__in=OPEN).exists()
    return is_open and bool(roles_of(user, thesis) & ALLOWED_ROLES)


def _months_later(day: dt.date, months: int) -> dt.date:
    import calendar

    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    return day.replace(
        year=year, month=month + 1, day=min(day.day, calendar.monthrange(year, month + 1)[1])
    )


def live(user: Any, thesis: Thesis) -> CalendarFeed | None:
    return CalendarFeed.objects.filter(
        user_id=user.pk, thesis_id=thesis.pk, revoked_at__isnull=True
    ).first()


def _audit(action: str, user: Any, thesis: Thesis, **details: Any) -> None:
    from apps.vault.audit import AuditAction, audit

    audit(AuditAction(action), actor=user, thesis=thesis, **details)


def _new(user: Any, thesis: Thesis) -> tuple[CalendarFeed, str]:
    token = new_token()  # 256 bits (vault.crypto: the only source of randomness)
    feed = CalendarFeed.objects.create(
        user=user,
        thesis=thesis,
        token_hash=hash_token(token),
        expires_on=_months_later(timezone.localdate(), LIFETIME_MONTHS),
        created_by=user,
    )
    return feed, token


def _check(user: Any, thesis: Thesis, recent_mfa: bool) -> None:
    if not allowed(user, thesis):
        raise PermissionDenied
    if not recent_mfa:
        raise PermissionDenied  # the views ask for the second factor first


def create(*, user: Any, thesis: Thesis, recent_mfa: bool) -> tuple[CalendarFeed, str]:
    """A new feed and its token, shown once. One live feed per (person, thesis)."""
    _check(user, thesis, recent_mfa)
    with transaction.atomic():
        if live(user, thesis) is not None:
            raise ValueError("a live feed exists: regenerate it")
        feed, token = _new(user, thesis)
        _audit("CALENDAR_FEED_CREATED", user, thesis)
    return feed, token


def regenerate(*, user: Any, thesis: Thesis, recent_mfa: bool) -> tuple[CalendarFeed, str]:
    """The old token dies at once; a new one is shown once."""
    _check(user, thesis, recent_mfa)
    with transaction.atomic():
        CalendarFeed.objects.filter(
            user_id=user.pk, thesis_id=thesis.pk, revoked_at__isnull=True
        ).update(revoked_at=timezone.now())
        feed, token = _new(user, thesis)
        _audit("CALENDAR_FEED_REGENERATED", user, thesis)
    return feed, token


def revoke(*, user: Any, thesis: Thesis) -> bool:
    """The person revokes their own feed (no second factor: a reduction of access)."""
    if not roles_of(user, thesis):
        raise PermissionDenied
    return revoke_for(user, thesis, reason="user", actor=user)


def revoke_for(user: Any, thesis: Thesis, *, reason: str, actor: Any = None) -> bool:
    """Revoke the live feed of ``user`` on ``thesis`` (automatic revocations too);
    journaled with ``reason`` only. True if there was one."""
    with transaction.atomic():
        count = CalendarFeed.objects.filter(
            user_id=user.pk, thesis_id=thesis.pk, revoked_at__isnull=True
        ).update(revoked_at=timezone.now())
        if count:
            _audit("CALENDAR_FEED_REVOKED", actor, thesis, reason=reason)
    return bool(count)


def find(token: str) -> CalendarFeed | None:
    """The live, unexpired feed of ``token`` on an open thesis, whose owner is an
    active member with an allowed role: ONE query, whatever the answer."""
    if not isinstance(token, str) or not 20 <= len(token) <= 64:
        return None
    digest = hash_token(token)
    allowed_membership = Membership.objects.active().filter(
        user_id=OuterRef("user_id"),
        thesis_id=OuterRef("thesis_id"),
        role__in=[r.value for r in ALLOWED_ROLES],
    )
    feed = (
        CalendarFeed.objects.filter(
            token_hash=digest,
            revoked_at__isnull=True,
            expires_on__gte=timezone.localdate(),
            thesis__status__in=OPEN,
            user__is_active=True,
        )
        .filter(Exists(allowed_membership))
        .select_related("user", "thesis")
        .first()
    )
    if feed is None or not tokens_match(token, feed.token_hash):
        return None
    return feed


def touch(feed: CalendarFeed) -> None:
    """last_used_on: the day only, written at most once a day."""
    today = timezone.localdate()
    if feed.last_used_on != today:
        CalendarFeed.objects.filter(pk=feed.pk).update(last_used_on=today)


def daily(today: dt.date | None = None) -> dict[str, int]:
    """Revoke the expired feeds and those of finished theses; warn 14 days before
    expiry (an « Échéances » activity event, a link only)."""
    from apps.activity import events
    from apps.activity.models import Event

    today = today or timezone.localdate()
    counts = {"expired": 0, "closed": 0, "notices": 0}
    live_feeds = CalendarFeed.objects.filter(revoked_at__isnull=True).select_related(
        "user", "thesis"
    )
    for feed in live_feeds:
        if feed.thesis.status not in OPEN:
            revoke_for(feed.user, feed.thesis, reason="thesis_closed")
            counts["closed"] += 1
        elif feed.expires_on < today:
            revoke_for(feed.user, feed.thesis, reason="expired")
            counts["expired"] += 1
        elif (
            feed.expires_on - today <= dt.timedelta(days=NOTICE_DAYS)
            and not Event.objects.filter(kind="CALENDAR_FEED_EXPIRING", object_id=feed.pk).exists()
        ):
            with transaction.atomic():
                events.record(
                    kind="CALENDAR_FEED_EXPIRING", obj=feed, object_type="FEED",
                    thesis=feed.thesis, actor=None,
                )  # fmt: skip
            counts["notices"] += 1
    return counts


def block(user: Any, thesis: Thesis) -> dict[str, Any] | None:
    """What the « Abonnement agenda » block shows (never the URL), or None."""
    if not allowed(user, thesis):
        return None
    return {"feed": live(user, thesis)}
