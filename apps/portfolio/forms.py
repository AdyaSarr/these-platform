"""Forms of the portfolio (M9a). They shape input only: rights, levels, bounds and
the certificate are decided again by portfolio.services."""

from __future__ import annotations

from typing import Any, cast

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.confidentiality import ConfidentialityLevel
from apps.vault.models import Document

from . import services
from .models import (
    AdumCategory,
    OutreachKind,
    SkillCategory,
    TeachingKind,
    TrainingCategory,
)

LEVEL_HELP = _(
    "Public: also read by the CSI members, who check your training. A training "
    "internal to ST: Internal ST."
)
CATEGORY_HELP = _("Categories to be confirmed by the doctoral school.")
LEVEL_CHOICES = [(level.value, level.label) for level in ConfidentialityLevel]


def _date() -> forms.DateInput:
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class TrainingForm(forms.Form):
    """Creation (with the level) or edition (without: it has its own form)."""

    title = forms.CharField(label=_("Title"), max_length=services.TEXT_LIMITS["title"][1])
    provider = forms.CharField(
        label=_("Provider"), required=False, max_length=services.TEXT_LIMITS["provider"][1]
    )
    category = forms.ChoiceField(
        label=_("Category"), choices=TrainingCategory.choices, help_text=CATEGORY_HELP
    )
    hours = forms.CharField(
        label=_("Hours"),
        max_length=6,
        help_text=_("In half hours, for example 3 or 12,5."),
    )
    start_date = forms.DateField(label=_("Start"), widget=_date())
    end_date = forms.DateField(label=_("End"), widget=_date())
    certificate = forms.ChoiceField(label=_("Certificate"), required=False)
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        max_length=services.TEXT_LIMITS["notes"][1],
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Read by you only."),
    )
    level = forms.ChoiceField(
        label=_("Confidentiality"),
        choices=LEVEL_CHOICES,
        initial=ConfidentialityLevel.PUBLIC.value,
        help_text=LEVEL_HELP,
    )

    def __init__(
        self, *args: Any, documents: list[Document], with_level: bool = True, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.documents = {str(d.pk): d for d in documents}
        field = cast(forms.ChoiceField, self.fields["certificate"])
        field.choices = [("", _("None"))] + [(key, d.title) for key, d in self.documents.items()]
        if not with_level:
            del self.fields["level"]

    def values(self) -> dict[str, Any]:
        data = self.cleaned_data
        return {
            "title": data["title"],
            "provider": data["provider"],
            "notes": data["notes"],
            "category": data["category"],
            "hours": data["hours"],
            "start_date": data["start_date"],
            "end_date": data["end_date"],
            "certificate": self.documents.get(data["certificate"] or ""),
        }


class LevelForm(forms.Form):
    level = forms.ChoiceField(label=_("Confidentiality"), choices=LEVEL_CHOICES)


class SettingsForm(forms.Form):
    reregistration_date = forms.DateField(
        label=_("Next re-registration"),
        required=False,
        widget=_date(),
        help_text=_(
            "The date given by the university; you receive a reminder 30 and 7 days before."
        ),
    )


# --------------------------------------------------------------------------- M9b

ED_HELP = _("Types to be confirmed by the doctoral school.")


class PublicationDetailsForm(forms.Form):
    adum_category = forms.ChoiceField(
        label=_("ADUM category"),
        choices=AdumCategory.choices,
        help_text=_("Categories to be confirmed by the doctoral school."),
    )
    presented_on = forms.DateField(
        label=_("Presented on"),
        required=False,
        widget=_date(),
        help_text=_("For a talk or a poster: the day of the communication."),
    )
    doi = forms.CharField(label=_("DOI"), required=False, max_length=200,
                          help_text=_("For example 10.1234/abcd.2027."))  # fmt: skip
    hal_id = forms.CharField(label=_("HAL identifier"), required=False, max_length=32,
                             help_text=_("For example hal-01234567v2."))  # fmt: skip
    volume = forms.CharField(label=_("Volume"), required=False, max_length=32)
    pages = forms.CharField(label=_("Pages"), required=False, max_length=32,
                            help_text=_("For example 101-117."))  # fmt: skip
    bibtex_key = forms.CharField(
        label=_("BibTeX key"),
        required=False,
        max_length=64,
        help_text=_("Letters, digits, « : », « _ » and « - » only."),
    )

    def values(self) -> dict[str, Any]:
        return dict(self.cleaned_data)


class TeachingForm(forms.Form):
    course = forms.CharField(label=_("Course"), max_length=services.TEXT_LIMITS["course"][1])
    institution = forms.CharField(
        label=_("Institution"), required=False,
        max_length=services.TEXT_LIMITS["institution"][1],
    )  # fmt: skip
    kind = forms.ChoiceField(label=_("Type"), choices=TeachingKind.choices, help_text=ED_HELP)
    hours = forms.CharField(
        label=_("Hours"), max_length=6, help_text=_("In half hours, for example 3 or 12,5.")
    )
    start_date = forms.DateField(label=_("Start"), widget=_date())
    end_date = forms.DateField(label=_("End"), widget=_date())
    notes = forms.CharField(
        label=_("Notes"), required=False, max_length=services.TEXT_LIMITS["notes"][1],
        widget=forms.Textarea(attrs={"rows": 3}), help_text=_("Read by you only."),
    )  # fmt: skip
    level = forms.ChoiceField(
        label=_("Confidentiality"),
        choices=LEVEL_CHOICES,
        initial=ConfidentialityLevel.PUBLIC.value,
        help_text=_("Read by the supervision at this level; never by the CSI members."),
    )

    def __init__(self, *args: Any, with_level: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not with_level:
            del self.fields["level"]

    def values(self) -> dict[str, Any]:
        return {k: v for k, v in self.cleaned_data.items() if k != "level"}


class OutreachForm(forms.Form):
    title = forms.CharField(label=_("Title"), max_length=services.TEXT_LIMITS["title"][1])
    audience = forms.CharField(
        label=_("Audience and place"), required=False,
        max_length=services.TEXT_LIMITS["audience"][1],
    )  # fmt: skip
    kind = forms.ChoiceField(label=_("Type"), choices=OutreachKind.choices, help_text=ED_HELP)
    date = forms.DateField(label=_("Date"), widget=_date())
    notes = forms.CharField(
        label=_("Notes"), required=False, max_length=services.TEXT_LIMITS["notes"][1],
        widget=forms.Textarea(attrs={"rows": 3}), help_text=_("Read by you only."),
    )  # fmt: skip
    level = forms.ChoiceField(
        label=_("Confidentiality"),
        choices=LEVEL_CHOICES,
        initial=ConfidentialityLevel.PUBLIC.value,
        help_text=_("Read by the supervision at this level; never by the CSI members."),
    )

    def __init__(self, *args: Any, with_level: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not with_level:
            del self.fields["level"]

    def values(self) -> dict[str, Any]:
        return {k: v for k, v in self.cleaned_data.items() if k != "level"}


class SkillForm(forms.Form):
    title = forms.CharField(label=_("Skill"), max_length=services.TEXT_LIMITS["title"][1])
    category = forms.ChoiceField(
        label=_("Category"), choices=SkillCategory.choices,
        help_text=_("Categories to be confirmed by the doctoral school."),
    )  # fmt: skip
    evidence = forms.CharField(
        label=_("What shows it"), required=False,
        max_length=services.TEXT_LIMITS["evidence"][1], widget=forms.Textarea(attrs={"rows": 3}),
    )  # fmt: skip

    def values(self) -> dict[str, Any]:
        return dict(self.cleaned_data)


class CareerForm(forms.Form):
    text = forms.CharField(
        label=_("Professional project"),
        required=False,
        max_length=services.TEXT_LIMITS["text"][1],
        widget=forms.Textarea(attrs={"rows": 10}),
        help_text=_("Read by you only: neither the supervision nor the company read it."),
    )
