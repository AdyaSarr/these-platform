"""Pages of the travel module (mounted under /t/<thesis_id>/travel/).

Thin views. Order of checks, by class order (as in M3 to M6):
1. ThesisPermissionRequiredMixin (403, THESIS_ACCESS_DENIED audited);
2. RecentAuthRequiredMixin (second factor < 10 min) for the settings: AFTER the
   guard, BEFORE View;
3. the service, which decides again, audits its own refusals and filters what each
   viewer sees (access.py): a template only renders what it is given.

Errors: invalid input answers 400 and renders the page with the message.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from fractions import Fraction
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import formats, timezone
from django.utils.dates import WEEKDAYS_ABBR
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.models import User
from apps.accounts.permissions import Action, ThesisPermissionRequiredMixin, can
from apps.accounts.reauth import RecentAuthRequiredMixin, authenticated_recently, reauth_response
from apps.objectives.years import year_count
from apps.vault.errors import ReauthenticationRequiredError

from . import services
from .access import PresenceAccess
from .choices import ShownKind, Side
from .forms import PresenceForm, RemindersForm, RulesForm, TargetForm

# Legend of the month page, per view (access.py): a viewer never reads the name of
# a kind they cannot see.
_LEGEND: dict[PresenceAccess, list[ShownKind]] = {
    PresenceAccess.FULL: [k for k in ShownKind if k != ShownKind.UNAVAILABLE],
    PresenceAccess.NO_LEAVE: [k for k in ShownKind if k != ShownKind.LEAVE],
    PresenceAccess.LIRMM_ONLY: [ShownKind.LIRMM],
}


def _user(request: HttpRequest) -> User:
    user = request.user
    if not isinstance(user, User):
        raise PermissionDenied
    return user


class _Mixin(ThesisPermissionRequiredMixin):
    required_action = Action.TRAVEL_READ
    request: HttpRequest

    def _url(self, name: str, **kw: Any) -> str:
        return reverse(f"travel:{name}", kwargs={"thesis_id": self.thesis.pk, **kw})

    def _month_url(self, year: int, month: int) -> str:
        return self._url("month", year=year, month=month)

    def _month_page(
        self,
        year: int,
        month: int,
        *,
        form: PresenceForm | None = None,
        status: int = 200,
        error: str = "",
    ) -> HttpResponse:
        user = _user(self.request)
        try:
            view = services.month_view(user, self.thesis, year, month)
        except ValidationError:
            raise Http404 from None
        writer = can(user, Action.TRAVEL_WRITE, self.thesis)
        labels = {k.value: k.label for k in ShownKind}
        weeks = [
            [
                None
                if cell is None
                else {
                    "day": cell.day,
                    "kind": cell.kind or "",
                    "label": labels[cell.kind] if cell.kind else "",
                    "working": cell.working,
                    "holiday": cell.holiday,
                    "today": cell.today,
                }
                for cell in week
            ]
            for week in view.weeks
        ]
        context = {
            "thesis": self.thesis,
            "view": view,
            "month_start": dt.date(view.year, view.month, 1),
            "weekdays": [WEEKDAYS_ABBR[i] for i in range(7)],
            "weeks": weeks,
            "legend": [(k.value, k.label) for k in _LEGEND[view.access]],
            "previous_url": self._month_url(*view.previous) if view.previous else "",
            "next_url": self._month_url(*view.next) if view.next else "",
            "form": (form or PresenceForm()) if writer else None,
            "error": error,
        }
        return render(self.request, "travel/month.html", context, status=status)


class CalendarView(_Mixin, View):
    """The month of today (Paris), or the nearest month of the thesis."""

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        low, high = services.bounds(self.thesis)
        today = min(max(timezone.localdate(), low), high)
        return self._month_page(today.year, today.month)


class MonthView(_Mixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._month_page(kwargs["year"], kwargs["month"])


class PresenceView(_Mixin, View):
    required_action = Action.TRAVEL_WRITE
    http_method_names: Sequence[str] = ("post",)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        form = PresenceForm(request.POST)
        today = timezone.localdate()
        if not form.is_valid():
            return self._month_page(today.year, today.month, form=form, status=400)
        data = form.cleaned_data
        try:
            changed = services.set_presence(
                thesis=self.thesis,
                user=_user(request),
                first=data["first"],
                last=data["last"],
                kind=data["kind"] or None,
                include_non_working=data["include_non_working"],
            )
        except ValidationError as exc:
            return self._month_page(
                today.year, today.month, form=form, status=400, error=" ".join(exc.messages)
            )
        messages.success(request, _("%(n)d days updated.") % {"n": changed})
        return redirect(self._month_url(data["first"].year, data["first"].month))


def _points(value: Fraction | None) -> str:
    """A percent or a gap, rounded for display only (the alert used the exact value)."""
    return "" if value is None else formats.number_format(round(float(value), 1), 1)


class SplitView(_Mixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        raw = request.GET.get("year")
        try:
            number = int(raw) if raw is not None else None
            summary = services.split_summary(_user(request), self.thesis, cifre_year=number)
        except (ValueError, ValidationError):
            raise Http404 from None
        years = range(1, summary.year_count + 1)
        context = {
            "thesis": self.thesis,
            "summary": summary,
            "full": summary.access is not PresenceAccess.LIRMM_ONLY,
            "st_percent": _points(summary.st_percent),
            "gap": _points(summary.gap),
            "counts": [
                (ShownKind(k).label, summary.shown_counts[k])
                for k in ShownKind.values
                if k in summary.shown_counts
            ],
            "years": [(n, f"{self._url('split')}?year={n}") for n in years],
        }
        return render(request, "travel/split.html", context)


class SettingsView(_Mixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._page()

    def _page(
        self,
        *,
        rules_form: RulesForm | None = None,
        target_form: TargetForm | None = None,
        reminders_form: RemindersForm | None = None,
        status: int = 200,
        error: str = "",
    ) -> HttpResponse:
        user = _user(self.request)
        policy = services.policy_of(self.thesis)
        rules_writer = can(user, Action.TRAVEL_RULES_SET, self.thesis)
        reminders_writer = can(user, Action.TRAVEL_REMINDERS_SET, self.thesis)
        targets = services.targets_of(self.thesis)
        count = year_count(self.thesis.cifre_start_date, self.thesis.planned_end_date)
        initial_rules = {name: getattr(policy, name) for name in policy.RULE_FIELDS}
        initial_reminders = {name: getattr(policy, name) for name in policy.REMINDER_FIELDS}
        context = {
            "thesis": self.thesis,
            "policy": policy,
            "remote_side": Side(policy.remote_counts_as).label,
            "conference_side": Side(policy.conference_counts_as).label,
            "targets": [(n, targets.get(n)) for n in range(1, count + 1)],
            "rules_form": (rules_form or RulesForm(initial=initial_rules))
            if rules_writer
            else None,
            "target_form": (target_form or TargetForm()) if rules_writer else None,
            "reminders_form": (reminders_form or RemindersForm(initial=initial_reminders))
            if reminders_writer
            else None,
            "error": error,
        }
        return render(self.request, "travel/settings.html", context, status=status)


class _SettingsPost(_Mixin, RecentAuthRequiredMixin, View):
    http_method_names: Sequence[str] = ("post",)
    form_class: type[Any]
    form_name: str

    def save(self, user: User, data: dict[str, Any], recent_mfa: bool) -> None:
        raise NotImplementedError

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        form = self.form_class(request.POST)
        page = SettingsView()
        page.request, page.thesis = request, self.thesis
        if not form.is_valid():
            return page._page(**{self.form_name: form}, status=400)
        try:
            self.save(_user(request), form.cleaned_data, authenticated_recently(request))
        except ReauthenticationRequiredError:
            return reauth_response(request, mfa=True)
        except ValidationError as exc:
            return page._page(**{self.form_name: form}, status=400, error=" ".join(exc.messages))
        messages.success(request, _("Settings saved."))
        return redirect(self._url("settings"))


class RulesView(_SettingsPost):
    required_action = Action.TRAVEL_RULES_SET
    form_class = RulesForm
    form_name = "rules_form"

    def save(self, user: User, data: dict[str, Any], recent_mfa: bool) -> None:
        services.set_rules(thesis=self.thesis, user=user, recent_mfa=recent_mfa, **data)


class TargetView(_SettingsPost):
    required_action = Action.TRAVEL_RULES_SET
    form_class = TargetForm
    form_name = "target_form"

    def save(self, user: User, data: dict[str, Any], recent_mfa: bool) -> None:
        services.set_target(thesis=self.thesis, user=user, recent_mfa=recent_mfa, **data)


class RemindersView(_SettingsPost):
    required_action = Action.TRAVEL_REMINDERS_SET
    form_class = RemindersForm
    form_name = "reminders_form"

    def save(self, user: User, data: dict[str, Any], recent_mfa: bool) -> None:
        services.set_reminders(thesis=self.thesis, user=user, recent_mfa=recent_mfa, **data)
