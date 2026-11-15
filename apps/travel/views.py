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
from dataclasses import dataclass
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
from django.utils.translation import gettext_lazy
from django.views import View

from apps.accounts.models import User
from apps.accounts.permissions import Action, ThesisPermissionRequiredMixin, can
from apps.accounts.reauth import RecentAuthRequiredMixin, authenticated_recently, reauth_response
from apps.admin_file.models import RequiredItem
from apps.conferences.models import Edition
from apps.core.confidentiality import ConfidentialityLevel
from apps.objectives.years import year_count
from apps.vault import services as vault
from apps.vault.errors import ReauthenticationRequiredError

from . import amounts, services
from .access import PresenceAccess
from .choices import Destination, MissionStatus, OrderDecision, ShownKind, Side, Transport
from .forms import (
    MissionForm,
    MissionLevelForm,
    OrderDecisionForm,
    OrderRequestForm,
    PresenceForm,
    RemindersForm,
    RulesForm,
    TargetForm,
)
from .models import ORDER_PAYERS, Mission, MissionOrder

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


# --------------------------------------------------------------------------- missions

CONFLICTS = frozenset(
    {
        "terminal",
        "not_returned",
        "order_required",
        "stale",
        "frozen",
        "already_pending",
        "decided",
        "order_pending",
        "no_decider",
        "no_order",
    }
)


def _status(exc: ValidationError) -> int:
    return 409 if getattr(exc, "code", None) in CONFLICTS else 400


@dataclass(frozen=True)
class Shown:
    text: str
    restricted: bool


def _shown(value: Any) -> Shown:
    if value is vault.RESTRICTED:
        return Shown(text="", restricted=True)
    return Shown(text=str(value or ""), restricted=False)


def _editions(thesis: Any) -> list[tuple[str, str]]:
    rows = Edition.objects.filter(thesis=thesis).select_related("venue").order_by("-year")
    return [(str(e.pk), f"{e.venue.acronym} {e.year}") for e in rows]


class _MissionMixin(_Mixin):
    kwargs: dict[str, Any]

    def _mission(self, operation: str) -> Mission:
        return services.mission_for(
            _user(self.request), self.thesis, self.kwargs["mission_id"], operation
        )

    def _mission_url(self, mission: Mission, name: str = "mission") -> str:
        return self._url(name, mission_id=mission.pk)


class RefusedLinkError(ValidationError):
    """A link id refused by services.link_for; ``data`` is the POST without it, to
    render the form again without echoing a refused id."""

    def __init__(self, data: Any) -> None:
        super().__init__(_("Unknown linked object."), code="unknown_link")
        self.data = data


def _links(user: User, thesis: Any, data: dict[str, Any], posted: Any) -> dict[str, Any]:
    """Form ids -> objects (services.link_for). Any refused id, whatever the case,
    is the same 400 with the same message and the same page."""
    out: dict[str, Any] = {}
    refused = []
    for kind in ("edition", "meeting", "submission"):
        raw = data.get(kind) or ""
        out[kind] = services.link_for(user, thesis, kind, raw)
        if raw and out[kind] is None:
            refused.append(kind)
    if refused:
        cleaned = posted.copy()
        for kind in refused:
            cleaned[kind] = ""
        raise RefusedLinkError(cleaned)
    return out


def _link_labels(meeting: Any, submission: Any) -> list[str]:
    """What the form says of its links: never a title (sealed), only what M5 and M6
    show in clear (a meeting's kind and date, a submission's conference)."""
    labels = []
    if meeting is not None:
        when = timezone.localtime(meeting.starts_at).strftime("%d/%m/%Y")
        labels.append(
            _("Meeting: %(kind)s, %(when)s") % {"kind": meeting.get_kind_display(), "when": when}
        )
    if submission is not None:
        target = submission.venue.acronym + (
            f" {submission.edition.year}" if submission.edition else ""
        )
        labels.append(_("Submission to %(target)s") % {"target": target})
    return labels


class MissionsView(_Mixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = _user(request)
        missions = services.mission_list(user, self.thesis)
        context = {
            "thesis": self.thesis,
            "missions": missions,
            "can_plan": can(user, Action.TRAVEL_WRITE, self.thesis),
        }
        return render(request, "travel/missions.html", context)


class NewMissionView(_Mixin, View):
    required_action = Action.TRAVEL_WRITE

    def _page(
        self, form: MissionForm, status: int = 200, error: str = "", links: list[str] | None = None
    ) -> HttpResponse:
        context = {
            "thesis": self.thesis,
            "form": form,
            "mission": None,
            "error": error,
            "links": links or [],
        }
        return render(self.request, "travel/mission_form.html", context, status=status)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = _user(request)
        initial: dict[str, Any] = {"payer": "ST", "level": "INTERNAL_ST"}
        found = {
            kind: services.link_for(user, self.thesis, kind, request.GET.get(kind, ""))
            for kind in ("meeting", "submission")
        }
        for kind, obj in found.items():
            if obj is not None:  # a refused id leaves the form as if none was given
                initial[kind] = str(obj.pk)
        form = MissionForm(initial=initial, editions=_editions(self.thesis), editing=False)
        return self._page(form, links=_link_labels(found["meeting"], found["submission"]))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        form = MissionForm(request.POST, editions=_editions(self.thesis), editing=False)
        if not form.is_valid():
            return self._page(form, status=400)
        data = form.cleaned_data
        try:
            mission = services.create_mission(
                thesis=self.thesis,
                user=_user(request),
                start_date=data["start_date"],
                end_date=data["end_date"],
                destination=data["destination"],
                payer=data["payer"],
                level=ConfidentialityLevel(data["level"]),
                transports=data["transports"],
                lodging=data["lodging"],
                destination_detail=data["destination_detail"],
                purpose=data["purpose"],
                lodging_note=data["lodging_note"],
                tickets_booked=data["tickets_booked"],
                lodging_booked=data["lodging_booked"],
                fill_presence=data["fill_presence"],
                **_links(_user(request), self.thesis, data, request.POST),
            )
        except RefusedLinkError as exc:
            form = MissionForm(exc.data, editions=_editions(self.thesis), editing=False)
            form.is_valid()
            return self._page(form, status=400, error=" ".join(exc.messages))
        except ValidationError as exc:
            return self._page(form, status=_status(exc), error=" ".join(exc.messages))
        messages.success(request, _("Mission planned."))
        return redirect(self._url("mission", mission_id=mission.pk))


class MissionView(_MissionMixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._page(self._mission("mission_read"))

    def _page(self, mission: Mission, status: int = 200, error: str = "") -> HttpResponse:
        user = _user(self.request)
        texts = services.mission_texts(user, mission)
        writer = can(user, Action.TRAVEL_WRITE, self.thesis)
        zrr = None
        if mission.destination == Destination.LIRMM and can(
            user, Action.ADMIN_FILE_READ, self.thesis
        ):
            zrr = RequiredItem.objects.filter(thesis=self.thesis, code="fsd_zrr_clearance").first()
        meeting = (
            mission.meeting
            if mission.meeting_id and can(user, Action.MEETING_READ, self.thesis)
            else None
        )
        submission_id = (
            mission.submission_id
            if mission.submission_id and can(user, Action.SUBMISSION_READ, self.thesis)
            else None
        )
        transports = dict(Transport.choices)
        context = {
            "thesis": self.thesis,
            "mission": mission,
            "texts": {name: _shown(value) for name, value in texts.items()},
            "transports": [transports[t] for t in mission.transports],
            "meeting": meeting,
            "submission_id": submission_id,
            "zrr": zrr,
            "writer": writer and not mission.is_closed,
            "level_form": MissionLevelForm(initial={"level": mission.level}) if writer else None,
            "orders": [
                {
                    "order": view.order,
                    "estimate": _amount(view.estimate),
                    "comment": _shown(view.comment),
                }
                for view in services.orders_of(user, mission)
            ],
            "request_form": OrderRequestForm()
            if writer
            and mission.payer in ORDER_PAYERS
            and mission.status in (MissionStatus.PLANNED, MissionStatus.ORDER_REFUSED)
            else None,
            "pending": next(
                (o for o in mission.orders.all() if o.decision == OrderDecision.PENDING), None
            ),
            "decision_form": OrderDecisionForm()
            if any(
                o.mission_id == mission.pk for o in services.pending_orders_for(user, self.thesis)
            )
            else None,
            "error": error,
        }
        return render(self.request, "travel/mission.html", context, status=status)


class EditMissionView(_MissionMixin, View):
    required_action = Action.TRAVEL_WRITE

    def _page(
        self, mission: Mission, form: MissionForm, status: int = 200, error: str = ""
    ) -> HttpResponse:
        context = {"thesis": self.thesis, "form": form, "mission": mission, "error": error}
        return render(self.request, "travel/mission_form.html", context, status=status)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        mission = self._mission("mission_update")
        texts = services.mission_texts(_user(request), mission)
        initial = {
            **{name: getattr(mission, name) for name in services.MISSION_FIELDS},
            **{name: str(value) for name, value in texts.items()},
            "edition": str(mission.edition_id or ""),
            "meeting": str(mission.meeting_id or ""),
            "submission": str(mission.submission_id or ""),
        }
        form = MissionForm(initial=initial, editions=_editions(self.thesis), editing=True)
        return self._page(mission, form)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        mission = self._mission("mission_update")
        form = MissionForm(request.POST, editions=_editions(self.thesis), editing=True)
        if not form.is_valid():
            return self._page(mission, form, status=400)
        data = form.cleaned_data
        try:
            changes = {
                **{name: data[name] for name in services.MISSION_FIELDS if name in data},
                **{name: data[name] for name in services.MISSION_TEXTS},
                **_links(_user(request), self.thesis, data, request.POST),
            }
            services.update_mission(mission=mission, user=_user(request), changes=changes)
        except RefusedLinkError as exc:
            form = MissionForm(exc.data, editions=_editions(self.thesis), editing=True)
            form.is_valid()
            return self._page(mission, form, status=400, error=" ".join(exc.messages))
        except ValidationError as exc:
            return self._page(mission, form, status=_status(exc), error=" ".join(exc.messages))
        messages.success(request, _("Mission saved."))
        return redirect(self._mission_url(mission))


class MissionLevelView(_MissionMixin, RecentAuthRequiredMixin, View):
    required_action = Action.TRAVEL_WRITE
    http_method_names: Sequence[str] = ("post",)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        mission = self._mission("mission_level")
        form = MissionLevelForm(request.POST)
        page = MissionView()
        page.request, page.thesis, page.kwargs = request, self.thesis, self.kwargs
        if not form.is_valid():
            return page._page(mission, status=400)
        try:
            services.change_mission_level(
                mission=mission,
                user=_user(request),
                level=ConfidentialityLevel(form.cleaned_data["level"]),
                recent_mfa=authenticated_recently(request),
            )
        except ReauthenticationRequiredError:
            return reauth_response(request, mfa=True)
        except ValidationError as exc:
            return page._page(mission, status=_status(exc), error=" ".join(exc.messages))
        messages.success(request, _("Confidentiality changed."))
        return redirect(self._mission_url(mission))


class _MissionAction(_MissionMixin, View):
    required_action = Action.TRAVEL_WRITE
    http_method_names: Sequence[str] = ("post",)
    done_message: Any = ""

    def act(self, mission: Mission, user: User) -> None:
        raise NotImplementedError

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        mission = self._mission("mission_status")
        try:
            self.act(mission, _user(request))
        except ValidationError as exc:
            page = MissionView()
            page.request, page.thesis, page.kwargs = request, self.thesis, self.kwargs
            return page._page(mission, status=_status(exc), error=" ".join(exc.messages))
        messages.success(request, self.done_message)
        return redirect(self._mission_url(mission))


class CancelMissionView(_MissionAction):
    done_message = gettext_lazy("Mission cancelled.")

    def act(self, mission: Mission, user: User) -> None:
        services.cancel_mission(mission=mission, user=user)


class MissionDoneView(_MissionAction):
    done_message = gettext_lazy("Mission marked as done.")

    def act(self, mission: Mission, user: User) -> None:
        services.mark_done(mission=mission, user=user)


# --------------------------------------------------------------------------- orders


@dataclass(frozen=True)
class ShownAmount:
    text: str
    restricted: bool


def _amount(value: Any) -> ShownAmount:
    if value is vault.RESTRICTED:
        return ShownAmount(text="", restricted=True)
    return ShownAmount(text="" if value is None else amounts.display(value), restricted=False)


def _mission_page(
    view: View, request: HttpRequest, mission: Mission, status: int, error: str
) -> HttpResponse:
    page = MissionView()
    page.request, page.thesis, page.kwargs = request, view.thesis, view.kwargs  # type: ignore[attr-defined]
    return page._page(mission, status=status, error=error)


class OrdersView(_Mixin, View):
    """The decider's queue: the pending orders this user decides."""

    required_action = Action.VALIDATE_MISSION_ORDER

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        orders = services.pending_orders_for(_user(request), self.thesis)
        return render(request, "travel/orders.html", {"thesis": self.thesis, "orders": orders})


class RequestOrderView(_MissionMixin, View):
    required_action = Action.TRAVEL_WRITE
    http_method_names: Sequence[str] = ("post",)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        mission = self._mission("order_request")
        form = OrderRequestForm(request.POST)
        if not form.is_valid():
            return _mission_page(
                self, request, mission, 400, _("An amount in euros, such as 123,45.")
            )
        try:
            services.request_order(
                mission=mission,
                user=_user(request),
                estimated_total=form.cleaned_data["estimated_total"],
            )
        except ValidationError as exc:
            return _mission_page(self, request, mission, _status(exc), " ".join(exc.messages))
        messages.success(request, _("Mission order requested."))
        return redirect(self._mission_url(mission))


class _OrderMixin(_Mixin):
    kwargs: dict[str, Any]

    def _order(self, operation: str) -> MissionOrder:
        return services.order_for(
            _user(self.request), self.thesis, self.kwargs["order_id"], operation
        )


class WithdrawOrderView(_OrderMixin, View):
    required_action = Action.TRAVEL_WRITE
    http_method_names: Sequence[str] = ("post",)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        order = self._order("order_withdraw")
        try:
            services.withdraw_order(order=order, user=_user(request))
        except ValidationError as exc:
            return _mission_page(self, request, order.mission, _status(exc), " ".join(exc.messages))
        messages.success(request, _("Mission order withdrawn."))
        return redirect(self._url("mission", mission_id=order.mission_id))


class _DeciderGuard:
    """Placed AFTER the thesis guard (which sets self.thesis) and BEFORE the second
    factor: only the payer's person who reads the mission goes on (a director on an
    ST order gets a 403, never a request for a second factor)."""

    request: HttpRequest
    kwargs: dict[str, Any]

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        self.kwargs = kwargs
        services.check_decider(_user(request), self._order("order_decide"))  # type: ignore[attr-defined]
        response: HttpResponseBase = super().dispatch(request, *args, **kwargs)  # type: ignore[misc]
        return response


class DecideOrderView(_OrderMixin, _DeciderGuard, RecentAuthRequiredMixin, View):
    required_action = Action.VALIDATE_MISSION_ORDER
    http_method_names: Sequence[str] = ("post",)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        order = self._order("order_decide")
        form = OrderDecisionForm(request.POST)
        if not form.is_valid():
            return _mission_page(self, request, order.mission, 400, _("Grant or refuse."))
        data = form.cleaned_data
        try:
            services.decide_order(
                order=order,
                user=_user(request),
                decision=data["decision"],
                reason=data["reason"],
                comment=data["comment"],
                recent_mfa=authenticated_recently(request),
            )
        except ReauthenticationRequiredError:
            return reauth_response(request, mfa=True)
        except ValidationError as exc:
            return _mission_page(self, request, order.mission, _status(exc), " ".join(exc.messages))
        messages.success(request, _("Decision recorded."))
        return redirect(self._url("mission", mission_id=order.mission_id))
