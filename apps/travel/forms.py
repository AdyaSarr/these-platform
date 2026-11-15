"""Forms of the travel module. They shape input only: rights, bounds and ranges are
decided again by travel.services."""

from __future__ import annotations

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from .choices import PresenceKind, Side
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
        label=_("Mission order to request (days before departure)"),
        min_value=0,
        max_value=MAX_ORDER_LEAD_DAYS,
    )
    receipt_grace_days = forms.IntegerField(
        label=_("Receipts expected (days after return)"),
        min_value=0,
        max_value=MAX_RECEIPT_GRACE_DAYS,
    )
