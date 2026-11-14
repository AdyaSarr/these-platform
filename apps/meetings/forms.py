"""Forms of the meetings. They shape input only: rights, levels, attendees and
states are decided again by meetings.services. Times are typed in Paris."""

from __future__ import annotations

import datetime as dt
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Thesis, User
from apps.accounts.permissions import can_access_level
from apps.core.confidentiality import ConfidentialityLevel
from apps.objectives.models import Objective

from . import services, timing
from .models import ActionItem, Meeting

TEXT_WARNING = _(
    "Sealed at the meeting's level: readers without this level see « Restricted content »."
)


def _date() -> forms.DateInput:
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


def _time() -> forms.TimeInput:
    return forms.TimeInput(attrs={"type": "time", "step": "60"}, format="%H:%M")


def _textarea(rows: int) -> forms.Textarea:
    return forms.Textarea(attrs={"rows": rows})


def level_choices(user: User, thesis: Thesis) -> list[tuple[str, Any]]:
    """The levels ``user`` may read: a writer cannot seal what they could not read."""
    return [
        (lv.value, lv.label) for lv in ConfidentialityLevel if can_access_level(user, thesis, lv)
    ]


def member_choices(thesis: Thesis) -> list[tuple[str, str]]:
    """Active internal members of the thesis (the possible attendees and assignees)."""
    users = [u for u in services.internal_members(thesis)]
    return [(str(u.pk), u.get_full_name() or u.email) for u in users]


class _WhenMixin(forms.Form):
    date = forms.DateField(label=_("Date"), widget=_date())
    time = forms.TimeField(label=_("Time (Paris)"), widget=_time(), input_formats=["%H:%M"])

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if isinstance(data.get("date"), dt.date) and isinstance(data.get("time"), dt.time):
            try:
                data["starts_at"] = timing.paris_datetime(data["date"], data["time"])
            except ValidationError as exc:
                self.add_error("time", exc)
        return data


class MeetingForm(_WhenMixin):
    """Creation (with the level) or edition (the level has its own form)."""

    kind = forms.ChoiceField(label=_("Type"), choices=Meeting.Kind.choices)
    presentation_venue = forms.ChoiceField(
        label=_("Presentation venue"),
        choices=[("", "—"), *Meeting.Venue.choices],
        required=False,
        help_text=_("Only for a presentation."),
    )
    title = forms.CharField(
        label=_("Title"), max_length=services.TEXT_LIMITS["title"][1], help_text=TEXT_WARNING
    )
    duration_minutes = forms.TypedChoiceField(
        label=_("Duration"),
        coerce=int,
        choices=[
            (m, f"{m // 60} h {m % 60:02d}" if m >= 60 else f"{m} min")
            for m in (15, 30, 45, 60, 90, 120, 180, 240, 480)
        ],
        initial=60,
    )
    location = forms.ChoiceField(label=_("Place"), choices=Meeting.Location.choices)
    location_detail = forms.CharField(
        label=_("Room or address"),
        required=False,
        max_length=services.TEXT_LIMITS["location_detail"][1],
    )
    visio_link = forms.CharField(
        label=_("Video link"),
        required=False,
        max_length=services.TEXT_LIMITS["visio_link"][1],
        help_text=_("https:// address; never copied into the calendar file."),
    )
    level = forms.ChoiceField(label=_("Confidentiality"))
    attendees = forms.MultipleChoiceField(
        label=_("Attendees"),
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Each attendee must be able to read the chosen level."),
    )

    field_order = [  # noqa: RUF012  (Django reads it on the class)
        "kind",
        "presentation_venue",
        "title",
        "date",
        "time",
        "duration_minutes",
        "location",
        "location_detail",
        "visio_link",
        "level",
        "attendees",
    ]

    def __init__(
        self, *args: Any, user: User, thesis: Thesis, with_level: bool = True, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.fields["attendees"].choices = member_choices(thesis)  # type: ignore[attr-defined]
        if with_level:
            self.fields["level"].choices = level_choices(user, thesis)  # type: ignore[attr-defined]
        else:
            del self.fields["level"]


class LevelForm(forms.Form):
    level = forms.ChoiceField(label=_("Confidentiality"))

    def __init__(self, *args: Any, user: User, thesis: Thesis, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["level"].choices = level_choices(user, thesis)  # type: ignore[attr-defined]


class DuplicateForm(_WhenMixin):
    pass


class AgendaForm(forms.Form):
    agenda = forms.CharField(
        label=_("Agenda"),
        required=False,
        max_length=services.TEXT_LIMITS["agenda"][1],
        widget=_textarea(6),
        help_text=TEXT_WARNING,
    )


class MinutesForm(forms.Form):
    minutes = forms.CharField(
        label=_("Minutes"),
        max_length=services.TEXT_LIMITS["minutes"][1],
        widget=_textarea(12),
        help_text=_("For a longer text, link a document of the vault."),
    )
    version = forms.IntegerField(widget=forms.HiddenInput, min_value=0)


class DecisionForm(forms.Form):
    text = forms.CharField(
        label=_("Decision"), max_length=services.TEXT_LIMITS["decision"][1], widget=_textarea(2)
    )


class ActionForm(forms.Form):
    title = forms.CharField(label=_("Action"), max_length=services.TEXT_LIMITS["action"][1])
    assignee = forms.ChoiceField(label=_("Person in charge"))
    due_date = forms.DateField(label=_("Due date"), widget=_date())
    objective = forms.ChoiceField(label=_("Linked objective"), required=False)

    def __init__(
        self, *args: Any, thesis: Thesis, objectives: list[tuple[str, str]], **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.thesis = thesis
        self.fields["assignee"].choices = member_choices(thesis)  # type: ignore[attr-defined]
        self.fields["objective"].choices = [("", "—"), *objectives]  # type: ignore[attr-defined]

    def clean_assignee(self) -> User:
        user = User.objects.filter(pk=self.cleaned_data["assignee"]).first()
        if user is None:
            raise ValidationError(_("Unknown person."), code="assignee")
        return user

    def clean_objective(self) -> Objective | None:
        raw = self.cleaned_data.get("objective")
        if not raw:
            return None
        objective = Objective.objects.filter(pk=raw, thesis=self.thesis).first()
        if objective is None:
            raise ValidationError(_("Unknown objective."), code="objective")
        return objective


class ActionStatusForm(forms.Form):
    status = forms.ChoiceField(label=_("Status"), choices=ActionItem.Status.choices)


class DocumentForm(forms.Form):
    document = forms.ChoiceField(label=_("Document of the vault"))

    def __init__(self, *args: Any, documents: list[tuple[str, str]], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["document"].choices = documents  # type: ignore[attr-defined]


class RegisterFilterForm(forms.Form):
    kind = forms.ChoiceField(
        label=_("Type"), choices=[("", _("All")), *Meeting.Kind.choices], required=False
    )
    start = forms.DateField(label=_("From"), required=False, widget=_date())
    end = forms.DateField(label=_("To"), required=False, widget=_date())
    withdrawn = forms.BooleanField(label=_("Include withdrawn decisions"), required=False)
