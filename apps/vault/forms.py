"""Forms of the document pages. Validation of the file itself is vault.validation's."""

from __future__ import annotations

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Thesis, User
from apps.accounts.permissions import can_access_level
from apps.core.confidentiality import ConfidentialityLevel

TITLE_WARNING = _("The title is not encrypted: do not put any confidential information in it.")


def _level_choices(user: User, thesis: Thesis) -> list[tuple[str, Any]]:
    return [
        (level.value, level.label)
        for level in ConfidentialityLevel
        if can_access_level(user, thesis, level)
    ]


def _set_level_choices(form: forms.Form, user: User, thesis: Thesis) -> None:
    field = form.fields["level"]
    if not isinstance(field, forms.ChoiceField):
        raise TypeError("level must be a ChoiceField")
    field.choices = _level_choices(user, thesis)


class UploadForm(forms.Form):
    title = forms.CharField(label=_("Title"), max_length=200, help_text=TITLE_WARNING)
    level = forms.ChoiceField(label=_("Confidentiality"))
    file = forms.FileField(label=_("File"))

    def __init__(self, *args: Any, user: User, thesis: Thesis, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _set_level_choices(self, user, thesis)


class NewVersionForm(forms.Form):
    file = forms.FileField(label=_("File"))


class LevelForm(forms.Form):
    level = forms.ChoiceField(label=_("Confidentiality"))

    def __init__(self, *args: Any, user: User, thesis: Thesis, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _set_level_choices(self, user, thesis)


class GrantForm(forms.Form):
    grantee = forms.UUIDField(widget=forms.HiddenInput)
