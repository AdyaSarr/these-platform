"""Forms of the objectives. They shape input only: rights, levels, reasons and
transitions are decided again by objectives.services (a reason, for instance, is
not required here: the service refuses its absence once the objective is approved)."""

from __future__ import annotations

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.confidentiality import ConfidentialityLevel

from . import services
from .models import OBJECTIVE_LEVELS, Category, ThesisYear

PUBLIC_WARNING = _(
    "Public: visible to the CSI members, who are outside the supervision; for any "
    "objective related to an ST product or result, choose Confidential."
)
REASON_HELP = _(
    "Required: this objective is approved. The reason is kept with the change and "
    "shown to the director."
)
LEVEL_CHOICES = [(level.value, level.label) for level in OBJECTIVE_LEVELS]
WEIGHT_CHOICES = [(1, "1"), (2, "2"), (3, "3")]


def _textarea(rows: int = 3) -> forms.Textarea:
    return forms.Textarea(attrs={"rows": rows})


def _date() -> forms.DateInput:
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class ObjectiveForm(forms.Form):
    """Creation (with the year and the level) or edition (with a reason once approved)."""

    year = forms.TypedChoiceField(label=_("CIFRE year"), coerce=int)
    title = forms.CharField(label=_("Title"), max_length=services.TEXT_LIMITS["title"][1])
    description = forms.CharField(
        label=_("Description"),
        required=False,
        max_length=services.TEXT_LIMITS["description"][1],
        widget=_textarea(4),
    )
    success_criterion = forms.CharField(
        label=_("Success criterion"),
        required=False,
        max_length=services.TEXT_LIMITS["success_criterion"][1],
        widget=_textarea(2),
    )
    category = forms.ChoiceField(label=_("Category"), choices=Category.choices)
    due_date = forms.DateField(label=_("Due date"), widget=_date())
    weight = forms.TypedChoiceField(
        label=_("Weight"), choices=WEIGHT_CHOICES, coerce=int, initial=1
    )
    level = forms.ChoiceField(
        label=_("Confidentiality"),
        choices=LEVEL_CHOICES,
        initial=ConfidentialityLevel.PUBLIC.value,
        help_text=PUBLIC_WARNING,
    )
    reason = forms.CharField(
        label=_("Reason for the change"),
        required=False,
        max_length=services.REASON_MAX,
        help_text=REASON_HELP,
        widget=_textarea(2),
    )

    def __init__(
        self,
        *args: Any,
        years: list[ThesisYear] | None = None,
        approved: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.creating = years is not None
        if years is not None:
            year = self.fields["year"]
            if not isinstance(year, forms.TypedChoiceField):
                raise TypeError("year must be a TypedChoiceField")
            year.choices = [(y.number, str(y.number)) for y in years]
            del self.fields["reason"]
        else:  # edition: year and level change elsewhere (level: second factor)
            del self.fields["year"]
            del self.fields["level"]
            if not approved:
                del self.fields["reason"]


class LevelForm(forms.Form):
    level = forms.ChoiceField(
        label=_("Confidentiality"), choices=LEVEL_CHOICES, help_text=PUBLIC_WARNING
    )
    reason = forms.CharField(
        label=_("Reason for the change"),
        required=False,
        max_length=services.REASON_MAX,
        widget=_textarea(2),
    )


class DropForm(forms.Form):
    reason = forms.CharField(
        label=_("Why is this objective dropped?"),
        required=False,  # the service refuses an empty or too short reason
        max_length=services.REASON_MAX,
        widget=_textarea(2),
    )


class ProgressForm(forms.Form):
    date = forms.DateField(label=_("Date"), widget=_date())
    progress = forms.IntegerField(label=_("Progress (%)"), min_value=0, max_value=100)
    status = forms.ChoiceField(
        label=_("Status"), choices=[(s.value, s.label) for s in services.PROGRESS_STATUSES]
    )
    note = forms.CharField(
        label=_("Note"), required=False, max_length=services.NOTE_MAX, widget=_textarea(2)
    )
    evidence = forms.ChoiceField(label=_("Evidence (optional)"), required=False)

    def __init__(self, *args: Any, documents: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.documents = documents or {}
        evidence = self.fields["evidence"]
        if not isinstance(evidence, forms.ChoiceField):
            raise TypeError("evidence must be a ChoiceField")
        evidence.choices = [("", "—"), *((pk, d.title) for pk, d in self.documents.items())]

    def clean_evidence(self) -> Any:
        chosen = self.cleaned_data.get("evidence")
        return self.documents.get(chosen) if chosen else None


class CommentForm(forms.Form):
    body = forms.CharField(label=_("Comment"), max_length=services.COMMENT_MAX, widget=_textarea(3))
