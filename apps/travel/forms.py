"""Forms of the travel module. They shape input only: rights, bounds and ranges are
decided again by travel.services."""

from __future__ import annotations

from typing import Any, cast

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.confidentiality import ConfidentialityLevel

from .choices import (
    Destination,
    Lodging,
    OrderDecision,
    OrderRefusalReason,
    Payer,
    PresenceKind,
    Side,
    Transport,
)
from .models import (
    MAX_GAP_ALERT_POINTS,
    MAX_LAB_DAYS_PER_MONTH,
    MAX_ORDER_LEAD_DAYS,
    MAX_RECEIPT_GRACE_DAYS,
)

ISO = ["%Y-%m-%d"]  # what <input type="date"> sends, whatever the language


def _date(label: Any) -> forms.DateField:
    return forms.DateField(
        label=label,
        input_formats=ISO,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )


class PresenceForm(forms.Form):
    first = _date(_("From"))
    last = _date(_("To"))
    kind = forms.ChoiceField(
        label=_("Kind"),
        choices=[("", _("— clear —")), *PresenceKind.choices],
        required=False,
    )
    include_non_working = forms.BooleanField(
        label=_("Also weekends and public holidays"), required=False
    )


class RulesForm(forms.Form):
    remote_counts_as = forms.ChoiceField(label=_("Remote work counts for"), choices=Side.choices)
    conference_counts_as = forms.ChoiceField(label=_("Conferences count for"), choices=Side.choices)
    whit_monday_holiday = forms.BooleanField(label=_("Whit Monday is a holiday"), required=False)
    gap_alert_points = forms.IntegerField(
        label=_("Alert when the gap exceeds (points)"), min_value=1, max_value=MAX_GAP_ALERT_POINTS
    )
    min_lab_days_per_month = forms.IntegerField(
        label=_("Minimum LIRMM days per month"),
        min_value=0,
        max_value=MAX_LAB_DAYS_PER_MONTH,
        required=False,
        help_text=_("Leave empty: no monthly rule."),
    )


class TargetForm(forms.Form):
    cifre_year = forms.IntegerField(label=_("CIFRE year"), min_value=1)
    st_percent = forms.IntegerField(label=_("ST share (percent)"), min_value=0, max_value=100)


class RemindersForm(forms.Form):
    order_lead_days = forms.IntegerField(
        label=_("Mission approval to request (days before departure)"),
        min_value=0,
        max_value=MAX_ORDER_LEAD_DAYS,
    )
    receipt_grace_days = forms.IntegerField(
        label=_("Receipts expected (days after return)"),
        min_value=0,
        max_value=MAX_RECEIPT_GRACE_DAYS,
    )


NO_ADDRESS = _(
    "Never an address: neither yours nor that of someone hosting you. "
    "Sealed at the mission's level."
)


class MissionForm(forms.Form):
    """Planning or editing a mission. The level has its own form (second factor) once
    the mission exists; links to a meeting or a submission come as hidden ids."""

    start_date = _date(_("Departure"))
    end_date = _date(_("Return"))
    destination = forms.ChoiceField(label=_("Destination"), choices=Destination.choices)
    destination_detail = forms.CharField(
        label=_("Place"),
        required=False,
        max_length=200,
        help_text=_("For another place only: a town or a site, never an address."),
    )
    edition = forms.ChoiceField(label=_("Conference"), required=False)
    payer = forms.ChoiceField(label=_("Paid by"), choices=Payer.choices)
    level = forms.ChoiceField(
        label=_("Confidentiality"),
        choices=ConfidentialityLevel.choices,
        help_text=_(
            "Paid by ST: readable by the tutor; paid by the LIRMM: readable by the director."
        ),
    )
    transports = forms.MultipleChoiceField(
        label=_("Transport"),
        choices=Transport.choices,
        widget=forms.CheckboxSelectMultiple,
        initial=[Transport.TRAIN.value],
    )
    lodging = forms.ChoiceField(label=_("Lodging"), choices=Lodging.choices)
    lodging_note = forms.CharField(
        label=_("Lodging note"),
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=NO_ADDRESS,
    )
    purpose = forms.CharField(
        label=_("Purpose"),
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=_("Sealed at the mission's level."),
    )
    tickets_booked = forms.BooleanField(label=_("Tickets booked"), required=False)
    lodging_booked = forms.BooleanField(label=_("Lodging booked"), required=False)
    official_order_obtained = forms.BooleanField(
        label=_("Official mission order obtained"),
        required=False,
        help_text=_("Issued by ST or the LIRMM, outside the platform."),
    )
    fill_presence = forms.BooleanField(
        label=_("Fill in the presence calendar (days already filled in are kept)"),
        required=False,
        initial=True,
    )
    # Plain text, checked by services.link_for: a UUIDField would answer a malformed id
    # with its own message, telling it apart from an unknown one.
    meeting = forms.CharField(required=False, max_length=64, widget=forms.HiddenInput)
    submission = forms.CharField(required=False, max_length=64, widget=forms.HiddenInput)

    def __init__(self, *args: Any, editions: list[tuple[str, str]], editing: bool, **kw: Any):
        super().__init__(*args, **kw)
        edition = cast(forms.ChoiceField, self.fields["edition"])  # declared above
        edition.choices = [("", "—"), *editions]
        if editing:  # the level has its own form; the calendar is filled at creation
            del self.fields["level"]
            del self.fields["fill_presence"]


class MissionLevelForm(forms.Form):
    level = forms.ChoiceField(label=_("Confidentiality"), choices=ConfidentialityLevel.choices)


class OrderRequestForm(forms.Form):
    estimated_total = forms.CharField(
        label=_("Estimated total (euros)"),
        max_length=20,
        help_text=_("Sealed at the mission's level. For example 312,40."),
    )


class OrderDecisionForm(forms.Form):
    decision = forms.ChoiceField(
        label=_("Decision"),
        choices=[
            (OrderDecision.GRANTED.value, OrderDecision.GRANTED.label),
            (OrderDecision.REFUSED.value, OrderDecision.REFUSED.label),
        ],
    )
    reason = forms.ChoiceField(
        label=_("Reason of a refusal"),
        choices=[("", "—"), *OrderRefusalReason.choices],
        required=False,
    )
    comment = forms.CharField(
        label=_("Comment"),
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Sealed at the mission's level; never in the e-mails."),
    )


class OfficialOrderForm(forms.Form):
    file = forms.FileField(
        label=_("Official mission order"),
        help_text=_("Stored encrypted in the vault, at the mission's level."),
    )
