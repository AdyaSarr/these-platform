from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import AnonymousUser
from django.db import models
from django.db.models import Q
from django.utils import timezone

if TYPE_CHECKING:
    from .models import Membership, Thesis, User  # noqa: F401  (string annotations)


class UserManager(BaseUserManager["User"]):
    # Makes the manager available inside migrations (RunPython).
    use_in_migrations = True

    @classmethod
    def normalize_email(cls, email: str | None) -> str:
        """Whole address lowercased: the e-mail is the login identifier."""
        return (email or "").strip().lower()

    def get_by_natural_key(self, username: str | None) -> User:
        # Used by authenticate(): login works whatever the case typed.
        return self.get(email=self.normalize_email(username))

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> User:
        email = self.normalize_email(email)
        if not email:
            raise ValueError("An email address is required.")
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        user = self.model(email=email, **extra)
        user.set_password(password)  # None -> unusable password
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if extra["is_staff"] is not True:
            raise ValueError("A superuser must have is_staff=True.")
        if extra["is_superuser"] is not True:
            raise ValueError("A superuser must have is_superuser=True.")
        return self.create_user(email, password, **extra)


class MembershipQuerySet(models.QuerySet["Membership"]):
    def active(self, at: datetime | None = None) -> MembershipQuerySet:
        """Memberships in force at ``at`` (default: now) of still-active users.

        The period is half-open, [active_from, active_until): a membership whose
        ``active_until`` is now has already ended.
        """
        at = at or timezone.now()
        return self.filter(
            Q(active_until__isnull=True) | Q(active_until__gt=at),
            active_from__lte=at,
            user__is_active=True,
        )


class ThesisQuerySet(models.QuerySet["Thesis"]):
    def visible_to(self, user: AbstractBaseUser | AnonymousUser) -> ThesisQuerySet:
        """Theses on which ``user`` holds at least one active membership."""
        if not user.is_authenticated or not user.is_active:
            return self.none()
        from .models import Membership

        active = Membership.objects.active().filter(user_id=user.pk)
        return self.filter(pk__in=active.values("thesis_id"))
