"""Forms of accounts."""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib.auth import password_validation
from django.utils.translation import gettext_lazy as _

from .models import User


class NewAccountForm(forms.Form):
    """Account creation for an invited address (the e-mail comes from the invitation)."""

    first_name = forms.CharField(label=_("First name"), max_length=150)
    last_name = forms.CharField(label=_("Last name"), max_length=150)
    password1 = forms.CharField(
        label=_("Password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text=password_validation.password_validators_help_text_html(),
    )
    password2 = forms.CharField(
        label=_("Password confirmation"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def __init__(self, *args: Any, email: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.email = email

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        p1, p2 = data.get("password1"), data.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", _("The two passwords do not match."))
        elif p1:
            candidate = User(
                email=self.email,
                first_name=data.get("first_name", ""),
                last_name=data.get("last_name", ""),
            )
            try:
                password_validation.validate_password(p1, candidate)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return data
