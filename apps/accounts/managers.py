from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.base_user import BaseUserManager

if TYPE_CHECKING:
    from .models import User


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
