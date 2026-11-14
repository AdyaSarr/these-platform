"""Forms of the conferences. They shape input only: rights, levels, versions and
states are decided again by conferences.services."""

from __future__ import annotations

import datetime as dt
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Thesis, User
from apps.accounts.permissions import can_access_level
from apps.core import timezones
from apps.core.confidentiality import ConfidentialityLevel
from apps.meetings.members import internal_members
from apps.vault.models import DocumentVersion

from . import services
from .models import (
    ApprovalPurpose,
    DeadlineKind,
    Edition,
    PreprintPlatform,
    RefusalReason,
    SubmissionStatus,
    Venue,
    VenueKind,
)

CLEAR_WARNING = _(
    "Public information, stored in clear text: never a title or a detail of your work."
)
SEALED_HELP = _("Sealed at the submission's level.")


def _date() -> forms.DateInput:
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


def _time() -> forms.TimeInput:
    return forms.TimeInput(attrs={"type": "time", "step": "60"}, format="%H:%M")


def _textarea(rows: int) -> forms.Textarea:
    return forms.Textarea(attrs={"rows": rows})


def _limit(name: str) -> int:
    return services.TEXT_LIMITS[name][1]


def level_choices(user: User, thesis: Thesis) -> list[tuple[str, Any]]:
    return [
        (lv.value, lv.label) for lv in ConfidentialityLevel if can_access_level(user, thesis, lv)
    ]


def member_choices(thesis: Thesis) -> list[tuple[str, str]]:
    return [(str(u.pk), u.get_full_name() or u.email) for u in internal_members(thesis)]


def target_choices(thesis: Thesis) -> list[tuple[str, str]]:
    """« Edition » or « journal » targets: ``edition:<id>`` or ``journal:<id>``."""
    choices: list[tuple[str, str]] = []
    for edition in (
        Edition.objects.filter(thesis=thesis)
        .select_related("venue")
        .order_by("venue__acronym", "year")
    ):
        choices.append((f"edition:{edition.pk}", f"{edition.venue.acronym} {edition.year}"))
    for venue in Venue.objects.filter(thesis=thesis, kind=VenueKind.JOURNAL).order_by("acronym"):
        choices.append((f"journal:{venue.pk}", f"{venue.acronym} ({venue.get_kind_display()})"))
    return choices


def resolve_target(thesis: Thesis, raw: str) -> tuple[Venue, Edition | None]:
    kind, _sep, pk = raw.partition(":")
    if kind == "edition":
        edition = Edition.objects.filter(pk=pk, thesis=thesis).select_related("venue").first()
        if edition is not None:
            return edition.venue, edition
    if kind == "journal":
        venue = Venue.objects.filter(pk=pk, thesis=thesis, kind=VenueKind.JOURNAL).first()
        if venue is not None:
            return venue, None
    raise ValidationError(_("Unknown target."), code="target")


class VenueForm(forms.Form):
    kind = forms.ChoiceField(label=_("Type"), choices=VenueKind.choices)
    acronym = forms.CharField(label=_("Acronym"), max_length=_limit("acronym"))
    name = forms.CharField(label=_("Name"), max_length=_limit("name"), help_text=CLEAR_WARNING)
    url = forms.CharField(label=_("Website (https)"), max_length=_limit("url"), required=False)


class EditionForm(forms.Form):
    year = forms.IntegerField(label=_("Year"), min_value=2000, max_value=2100)
    location = forms.CharField(label=_("Place"), max_length=_limit("location"), required=False)
    event_start = forms.DateField(label=_("First day"), required=False, widget=_date())
    event_end = forms.DateField(label=_("Last day"), required=False, widget=_date())


class DeadlineForm(forms.Form):
    kind = forms.ChoiceField(label=_("Type"), choices=DeadlineKind.choices)
    day = forms.DateField(label=_("Date (in the deadline's time zone)"), widget=_date())
    wall = forms.TimeField(
        label=_("Time (in the deadline's time zone)"),
        widget=_time(),
        input_formats=["%H:%M"],
        initial=dt.time(23, 59),
    )
    tz_name = forms.ChoiceField(
        label=_("Time zone"),
        choices=timezones.ZONE_CHOICES,
        initial=timezones.AOE,
        help_text=_("AoE: Anywhere on Earth (UTC-12), as most calls for papers say."),
    )


class SubmissionForm(forms.Form):
    """Creation (with the level) or edition (the level has its own form)."""

    target = forms.ChoiceField(label=_("Conference edition or journal"))
    title = forms.CharField(label=_("Title"), max_length=_limit("title"), help_text=SEALED_HELP)
    abstract = forms.CharField(
        label=_("Abstract"), max_length=_limit("abstract"), required=False, widget=_textarea(5)
    )
    external_authors = forms.CharField(
        label=_("External co-authors"),
        max_length=_limit("external_authors"),
        required=False,
        widget=_textarea(2),
        help_text=_("Names only, one per line."),
    )
    notes = forms.CharField(
        label=_("Notes"), max_length=_limit("notes"), required=False, widget=_textarea(3)
    )
    objective = forms.ChoiceField(label=_("Linked objective"), required=False)
    authors = forms.MultipleChoiceField(
        label=_("Internal co-authors"),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text=_("Each co-author must be able to read the chosen level."),
    )
    level = forms.ChoiceField(
        label=_("Confidentiality"),
        help_text=_(
            "ST work (the default) must be readable by the industrial tutor: Public, "
            "Internal ST or Confidential."
        ),
    )

    def __init__(
        self,
        *args: Any,
        user: User,
        thesis: Thesis,
        objectives: list[tuple[str, str]],
        with_level: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.thesis = thesis
        self.fields["target"].choices = target_choices(thesis)  # type: ignore[attr-defined]
        self.fields["objective"].choices = [("", "—"), *objectives]  # type: ignore[attr-defined]
        self.fields["authors"].choices = member_choices(thesis)  # type: ignore[attr-defined]
        if with_level:
            self.fields["level"].choices = level_choices(user, thesis)  # type: ignore[attr-defined]
        else:
            del self.fields["level"]

    def clean_target(self) -> tuple[Venue, Edition | None]:
        return resolve_target(self.thesis, self.cleaned_data["target"])


class LevelForm(forms.Form):
    level = forms.ChoiceField(label=_("Confidentiality"))

    def __init__(self, *args: Any, user: User, thesis: Thesis, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["level"].choices = level_choices(user, thesis)  # type: ignore[attr-defined]


class DocumentForm(forms.Form):
    document = forms.ChoiceField(label=_("Document of the vault"))

    def __init__(self, *args: Any, documents: list[tuple[str, str]], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["document"].choices = documents  # type: ignore[attr-defined]


class _VersionMixin(forms.Form):
    version = forms.ChoiceField(label=_("Version"))

    def __init__(self, *args: Any, versions: list[DocumentVersion], required: bool, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._versions = {str(v.pk): v for v in versions}
        field = self.fields["version"]
        field.required = required
        field.choices = [  # type: ignore[attr-defined]
            *([] if required else [("", "—")]),
            *((str(v.pk), f"{v.document.title} — v{v.version}") for v in versions),
        ]

    def clean_version(self) -> DocumentVersion | None:
        raw = self.cleaned_data.get("version")
        return self._versions.get(raw) if raw else None


class RequestForm(_VersionMixin):
    purpose = forms.ChoiceField(label=_("For"), choices=ApprovalPurpose.choices)


class StatusForm(_VersionMixin):
    status = forms.ChoiceField(
        label=_("New status"),
        choices=[
            (s.value, s.label)
            for s in SubmissionStatus
            if s not in (SubmissionStatus.ST_REQUESTED, SubmissionStatus.ST_GRANTED)
        ],
    )


class PreprintForm(_VersionMixin):
    platform = forms.ChoiceField(label=_("Platform"), choices=PreprintPlatform.choices)
    posted_on = forms.DateField(label=_("Posted on"), widget=_date())
    url = forms.CharField(
        label=_("Public address (https)"), max_length=_limit("preprint_url"), help_text=SEALED_HELP
    )


class DecisionForm(forms.Form):
    decision = forms.ChoiceField(
        label=_("Decision"), choices=[("GRANTED", _("Grant")), ("REFUSED", _("Refuse"))]
    )
    reason = forms.ChoiceField(
        label=_("Reason of a refusal"), choices=[("", "—"), *RefusalReason.choices], required=False
    )
    comment = forms.CharField(
        label=_("Comment"),
        max_length=_limit("comment"),
        required=False,
        widget=_textarea(3),
        help_text=SEALED_HELP,
    )


class ResubmitForm(forms.Form):
    target = forms.ChoiceField(label=_("New conference edition or journal"))

    def __init__(self, *args: Any, thesis: Thesis, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.thesis = thesis
        self.fields["target"].choices = target_choices(thesis)  # type: ignore[attr-defined]

    def clean_target(self) -> tuple[Venue, Edition | None]:
        return resolve_target(self.thesis, self.cleaned_data["target"])


class PolicyForm(forms.Form):
    st_review_days = forms.IntegerField(
        label=_("ST review period (days)"), min_value=0, max_value=180
    )
