"""Forms of the administrative file. They shape input only: every rule is decided
again by admin_file.services (rights, levels, validators, transitions)."""

from __future__ import annotations

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel

from . import services
from .models import VALIDATOR_ROLES, Reason, RequiredItem, Section

CLEAR_TEXT_WARNING = _(
    "This comment is not encrypted and is shown to the supervisors of the section: "
    "no personal information (nationality, health, amounts)."
)
LABEL_WARNING = _(
    "The label is not encrypted and is shown to the supervisors of the section: "
    "no personal information (nationality, health, amounts). Example: "
    "“Residence permit renewal”, never the nationality."
)
ATTACH_WARNING = {
    Section.ACADEMIC: _(
        "Once attached, co-supervisors and the industrial tutor will no longer be able to "
        "read it from the Documents page: only those who see this part of the file can."
    ),
    Section.CIFRE: _(
        "Once attached, co-supervisors will no longer be able to read it from the "
        "Documents page: only those who see this part of the file can."
    ),
}


class SubmitForm(forms.Form):
    file = forms.FileField(label=_("New file"), required=False)
    document = forms.ChoiceField(label=_("Or an existing document"), required=False)
    level = forms.ChoiceField(label=_("Confidentiality"), required=False)

    def __init__(self, *args: Any, item: RequiredItem, user: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.item = item
        self.candidates = {str(d.pk): d for d in services.attachable_documents(user, item)}
        document = self.fields["document"]
        level = self.fields["level"]
        if not isinstance(document, forms.ChoiceField) or not isinstance(level, forms.ChoiceField):
            raise TypeError("document and level must be ChoiceFields")
        document.choices = [("", "—"), *((pk, d.title) for pk, d in self.candidates.items())]
        document.help_text = ATTACH_WARNING[Section(item.section)]
        level.choices = [(lv.value, lv.label) for lv in services.allowed_levels(item)]
        level.initial = item.default_level
        if item.document is not None:  # a new version keeps the document's level
            del self.fields["level"]
            del self.fields["document"]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if data.get("file") and data.get("document"):
            raise forms.ValidationError(_("Give a new file or an existing document, not both."))
        chosen = data.get("document")
        data["document_obj"] = self.candidates.get(chosen) if chosen else None
        return data


class DecisionForm(forms.Form):
    reason = forms.ChoiceField(label=_("Reason"), choices=[("", "—"), *Reason.choices])
    comment = forms.CharField(
        label=_("Comment"),
        required=False,
        max_length=services.COMMENT_MAX,
        help_text=CLEAR_TEXT_WARNING,
        widget=forms.TextInput,
    )


class DueDateForm(forms.Form):
    due_date = forms.DateField(
        label=_("Due date"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )


def _validator_choices() -> list[tuple[str, str]]:
    return [
        (",".join(roles), " + ".join(str(Role(r).label) for r in roles))
        for roles in VALIDATOR_ROLES
    ]


class CustomItemForm(forms.Form):
    label = forms.CharField(
        label=_("Label"), max_length=services.LABEL_MAX, help_text=LABEL_WARNING
    )
    section = forms.ChoiceField(label=_("Part of the file"), choices=Section.choices)
    validators = forms.CharField(label=_("Validated by"))
    default_level = forms.ChoiceField(
        label=_("Default confidentiality"),
        choices=ConfidentialityLevel.choices,
        initial=ConfidentialityLevel.CONFIDENTIAL,
    )
    mandatory = forms.BooleanField(label=_("Mandatory"), required=False)
    attachment_required = forms.BooleanField(label=_("Attachment required"), required=False)
    due_date = forms.DateField(
        label=_("Due date"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["validators"].widget = forms.Select(choices=_validator_choices())

    def clean_validators(self) -> list[Role]:
        raw = self.cleaned_data["validators"]
        try:
            roles = [Role(r) for r in raw.split(",")]
        except ValueError:
            raise forms.ValidationError(_("These validators are not allowed.")) from None
        if tuple(roles) not in VALIDATOR_ROLES:
            raise forms.ValidationError(_("These validators are not allowed."))
        return roles

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if {"section", "validators", "default_level"} <= data.keys():
            try:
                services.check_validators(
                    Section(data["section"]),
                    data["validators"],
                    ConfidentialityLevel(data["default_level"]),
                )
            except forms.ValidationError as exc:
                raise forms.ValidationError(exc.messages) from None
        return data
