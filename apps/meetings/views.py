"""Pages of the meetings (mounted under /t/<thesis_id>/meetings/).

Thin views. Order of checks, by class order (as in M3 and M4):
1. ThesisPermissionRequiredMixin (403, THESIS_ACCESS_DENIED audited);
2. RecentAuthRequiredMixin (second factor < 10 min) for approving the minutes
   and changing the level: AFTER the guard (403 before any re-authentication)
   and BEFORE View;
3. the service, which decides again (author or attendee, level) and audits its
   own refusals.

Errors: a state conflict answers 409, invalid input 400; both render the detail
page with the message. URLs carry ids only, page titles are generic: a meeting's
title never leaves the body of the page that shows it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views import View

from apps.accounts.models import User
from apps.accounts.permissions import (
    Action,
    ThesisPermissionRequiredMixin,
    can,
    can_access_document,
)
from apps.accounts.reauth import RecentAuthRequiredMixin, authenticated_recently, reauth_response
from apps.core.confidentiality import ConfidentialityLevel
from apps.objectives.models import Objective
from apps.vault import services as vault
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import Document

from . import ics, services, timing
from .forms import (
    ActionForm,
    ActionStatusForm,
    AgendaForm,
    DecisionForm,
    DocumentForm,
    DuplicateForm,
    LevelForm,
    MeetingForm,
    MinutesForm,
    RegisterFilterForm,
)
from .models import ActionItem, Decision, Meeting, MeetingDocument, MinutesAcknowledgement

CONFLICTS = frozenset(
    {
        "stale",
        "frozen",
        "cancelled",
        "csi_pending",
        "no_minutes",
        "already_acknowledged",
        "already_linked",
        "withdrawn",
        "nothing_to_prefill",
        "not_started",
    }
)


def _user(request: HttpRequest) -> User:
    user = request.user
    if not isinstance(user, User):  # the thesis guard already refused anonymous users
        raise PermissionDenied
    return user


def _status(exc: ValidationError) -> int:
    return 409 if getattr(exc, "code", None) in CONFLICTS else 400


@dataclass(frozen=True)
class Shown:
    """A sealed text as the page shows it: the text, or « Restricted content »."""

    text: str
    restricted: bool


def shown(value: Any) -> Shown:
    if value is vault.RESTRICTED:
        return Shown(text="", restricted=True)
    return Shown(text=str(value or ""), restricted=False)


def _documents(user: User, thesis: Any) -> list[tuple[str, str]]:
    candidates = Document.objects.filter(thesis=thesis).order_by("title")
    return [(str(d.pk), d.title) for d in candidates if can_access_document(user, d)]


def _objectives(user: User, thesis: Any) -> list[tuple[str, str]]:
    """Objectives to link an action to, by their title when the user reads it."""
    objectives = list(Objective.objects.filter(thesis=thesis).order_by("due_date", "pk"))
    opened = (
        vault.open_texts(
            user, thesis, [(o, "title") for o in objectives], action=Action.OBJECTIVE_READ
        )
        if can(user, Action.OBJECTIVE_READ, thesis)
        else {}
    )
    choices = []
    for o in objectives:
        title = opened.get((o.pk, "title"), "")
        label = _("Restricted content") if title is vault.RESTRICTED else str(title)
        choices.append((str(o.pk), f"{label} ({o.due_date:%d/%m/%Y})"))
    return choices


def _meeting_url(thesis: Any, meeting: Meeting) -> str:
    return reverse("meetings:detail", kwargs={"thesis_id": thesis.pk, "meeting_id": meeting.pk})


def _initial(meeting: Meeting, texts: dict[str, Any]) -> dict[str, Any]:
    day, wall = timing.paris_parts(meeting.starts_at)
    return {
        "kind": meeting.kind,
        "presentation_venue": meeting.presentation_venue,
        "title": shown(texts["title"]).text,
        "date": day,
        "time": wall,
        "duration_minutes": meeting.duration_minutes,
        "location": meeting.location,
        "location_detail": shown(texts["location_detail"]).text,
        "visio_link": shown(texts["visio_link"]).text,
        "attendees": [str(pk) for pk in meeting.attendees.values_list("user_id", flat=True)],
    }


class _Mixin(ThesisPermissionRequiredMixin):
    required_action = Action.MEETING_READ
    request: HttpRequest

    def _meeting(self, meeting_id: Any, operation: str) -> Meeting:
        return services.meeting_for(
            _user(self.request), self.thesis, meeting_id, operation=operation
        )

    def _detail(
        self, meeting: Meeting, *, status: int = 200, error: str = "", **forms: Any
    ) -> HttpResponse:
        user = _user(self.request)
        detail = services.meeting_detail(user, meeting)
        m = detail.meeting
        writer = detail.can_write and not m.is_cancelled
        texts = {name: shown(value) for name, value in detail.texts.items()}
        readable = not texts["title"].restricted
        csi_pending = m.is_csi
        started = m.starts_at <= timezone.now()
        context = {
            "thesis": self.thesis,
            "meeting": m,
            "texts": texts,
            "attendees": [
                {
                    "user": u,
                    "mark": detail.marks.get(u.pk, ""),
                    "older": detail.older_marks.get(u.pk, ""),
                }
                for u in detail.attendees
            ],
            "former_attendees": [{"user": u} for u in detail.former_attendees],
            "decisions": [(d.decision, shown(d.text)) for d in detail.decisions],
            "actions": [
                {
                    "action": a.action,
                    "title": shown(a.title),
                    "overdue": a.overdue,
                    "can_status": a.action.assignee_id == user.pk or detail.can_write,
                }
                for a in detail.actions
            ],
            "documents": [(d, detail.document_links[d.pk]) for d in detail.documents],
            "hidden_documents": detail.hidden_documents,
            "can_write": writer and readable,
            "csi_pending": csi_pending,
            "can_acknowledge": detail.can_acknowledge
            and m.minutes_version > 0
            and not csi_pending
            and started,
            "started": started,
            "my_mark": detail.marks.get(user.pk, ""),
            "status_choices": ActionItem.Status.choices,
            "error": error,
        }
        if context["can_write"]:
            context["agenda_form"] = forms.get("agenda_form") or AgendaForm(
                initial={"agenda": texts["agenda"].text}
            )
            if not csi_pending and started:
                context["minutes_form"] = forms.get("minutes_form") or MinutesForm(
                    initial={"minutes": texts["minutes"].text, "version": m.minutes_version}
                )
                context["decision_form"] = forms.get("decision_form") or DecisionForm()
                context["action_form"] = forms.get("action_form") or ActionForm(
                    thesis=self.thesis, objectives=_objectives(user, self.thesis)
                )
            context["document_form"] = forms.get("document_form") or DocumentForm(
                documents=_documents(user, self.thesis)
            )
            context["level_form"] = forms.get("level_form") or LevelForm(
                user=user, thesis=self.thesis, initial={"level": m.level}
            )
        return render(self.request, "meetings/detail.html", context, status=status)


class _MeetingGuard:
    """Per-meeting rule BEFORE the re-authentication: a non-attendee (or a
    non-writer) gets 403 without being asked for a second factor. Placed after
    ThesisPermissionRequiredMixin and before RecentAuthRequiredMixin (test of
    the method resolution order)."""

    object_rule = "writer"  # or "attendee"
    thesis: Any

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        user = _user(request)
        operation = getattr(self, "operation", "") or "guard"
        meeting = services.meeting_for(
            user, self.thesis, kwargs.get("meeting_id"), operation=operation
        )
        if self.object_rule == "attendee":
            services.require_attendee(user, meeting, operation)
        else:
            services.require_writer(user, meeting, operation)
        response: HttpResponseBase = super().dispatch(request, *args, **kwargs)  # type: ignore[misc]
        return response


# --------------------------------------------------------------------------- lists


class ListView(_Mixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        past = request.GET.get("when") == "past"
        kind = request.GET.get("kind", "")
        kind = kind if kind in Meeting.Kind.values else ""
        rows = services.meeting_list(_user(request), self.thesis, past=past, kind=kind or None)
        context = {
            "thesis": self.thesis,
            "rows": [(r.meeting, shown(r.title), r.is_attendee) for r in rows],
            "past": past,
            "kind": kind,
            "kinds": Meeting.Kind.choices,
            "can_create": can(_user(request), Action.MEETING_WRITE, self.thesis),
        }
        return render(request, "meetings/list.html", context)


class RegisterView(_Mixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = RegisterFilterForm(request.GET or None)
        filters: dict[str, Any] = {}
        status = 200
        if form.is_bound:
            if form.is_valid():
                data = form.cleaned_data
                filters = {
                    "kind": data["kind"] or None,
                    "start": data["start"],
                    "end": data["end"],
                    "withdrawn": data["withdrawn"],
                }
            else:
                status = 400
        rows = (
            services.decision_register(_user(request), self.thesis, **filters)
            if status == 200
            else []
        )
        context = {
            "thesis": self.thesis,
            "form": form,
            "rows": [(r.decision, r.meeting, shown(r.text), shown(r.meeting_title)) for r in rows],
        }
        return render(request, "meetings/register.html", context, status=status)


# --------------------------------------------------------------------------- create, edit


class NewView(_Mixin, View):
    required_action = Action.MEETING_WRITE

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = _user(request)
        form = MeetingForm(user=user, thesis=self.thesis, initial={"attendees": [str(user.pk)]})
        return self._page(form)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        user = _user(request)
        form = MeetingForm(request.POST, user=user, thesis=self.thesis)
        if not form.is_valid():
            return self._page(form, status=400)
        data = form.cleaned_data
        try:
            meeting = services.create_meeting(
                thesis=self.thesis,
                user=user,
                kind=data["kind"],
                presentation_venue=data["presentation_venue"],
                starts_at=data["starts_at"],
                duration_minutes=data["duration_minutes"],
                location=data["location"],
                level=ConfidentialityLevel(data["level"]),
                title=data["title"],
                location_detail=data["location_detail"],
                visio_link=data["visio_link"],
                attendees=User.objects.filter(pk__in=data["attendees"]),
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
            return self._page(form, status=_status(exc))
        messages.success(request, _("Meeting created."))
        return redirect(_meeting_url(self.thesis, meeting))

    def _page(self, form: MeetingForm, status: int = 200) -> HttpResponse:
        context = {"thesis": self.thesis, "form": form, "meeting": None}
        return render(self.request, "meetings/form.html", context, status=status)


class EditView(_Mixin, View):
    required_action = Action.MEETING_WRITE

    def get(self, request: HttpRequest, *args: Any, meeting_id: Any, **kwargs: Any) -> HttpResponse:
        user = _user(request)
        meeting = self._meeting(meeting_id, "edit")
        if not services.is_writer(user, meeting):
            raise PermissionDenied
        texts = services.meeting_detail(user, meeting).texts
        form = MeetingForm(
            user=user, thesis=self.thesis, with_level=False, initial=_initial(meeting, texts)
        )
        return self._page(meeting, form)

    def post(
        self, request: HttpRequest, *args: Any, meeting_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        user = _user(request)
        meeting = self._meeting(meeting_id, "edit")
        form = MeetingForm(request.POST, user=user, thesis=self.thesis, with_level=False)
        if not form.is_valid():
            return self._page(meeting, form, status=400)
        data = form.cleaned_data
        changes = {
            "kind": data["kind"],
            "presentation_venue": data["presentation_venue"],
            "starts_at": data["starts_at"],
            "duration_minutes": data["duration_minutes"],
            "location": data["location"],
            "title": data["title"],
            "location_detail": data["location_detail"],
            "visio_link": data["visio_link"],
        }
        try:
            services.update_meeting(meeting=meeting, user=user, changes=changes)
            services.set_attendees(
                meeting=meeting, user=user, attendees=User.objects.filter(pk__in=data["attendees"])
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
            return self._page(meeting, form, status=_status(exc))
        messages.success(request, _("Meeting saved."))
        return redirect(_meeting_url(self.thesis, meeting))

    def _page(self, meeting: Meeting, form: MeetingForm, status: int = 200) -> HttpResponse:
        context = {"thesis": self.thesis, "form": form, "meeting": meeting}
        return render(self.request, "meetings/form.html", context, status=status)


class DuplicateView(_Mixin, View):
    required_action = Action.MEETING_WRITE

    def get(self, request: HttpRequest, *args: Any, meeting_id: Any, **kwargs: Any) -> HttpResponse:
        meeting = self._meeting(meeting_id, "duplicate")
        services.require_writer(_user(request), meeting, "duplicate")
        day, wall = timing.paris_parts(meeting.starts_at)
        form = DuplicateForm(initial={"date": day, "time": wall})
        return self._page(meeting, form)

    def post(
        self, request: HttpRequest, *args: Any, meeting_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        meeting = self._meeting(meeting_id, "duplicate")
        form = DuplicateForm(request.POST)
        if not form.is_valid():
            return self._page(meeting, form, status=400)
        try:
            copy = services.duplicate_meeting(
                meeting=meeting, user=_user(request), starts_at=form.cleaned_data["starts_at"]
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
            return self._page(meeting, form, status=_status(exc))
        messages.success(request, _("Meeting duplicated."))
        return redirect(_meeting_url(self.thesis, copy))

    def _page(self, meeting: Meeting, form: DuplicateForm, status: int = 200) -> HttpResponse:
        context = {"thesis": self.thesis, "form": form, "meeting": meeting}
        return render(self.request, "meetings/duplicate.html", context, status=status)


class DetailView(_Mixin, View):
    def get(self, request: HttpRequest, *args: Any, meeting_id: Any, **kwargs: Any) -> HttpResponse:
        return self._detail(self._meeting(meeting_id, "detail"))


class IcsView(_Mixin, View):
    """The .ics of one meeting, for a signed-in reader: labels only (meetings.ics)."""

    def get(self, request: HttpRequest, *args: Any, meeting_id: Any, **kwargs: Any) -> HttpResponse:
        user = _user(request)
        meeting = self._meeting(meeting_id, "ics")
        services.record_ics_export(meeting=meeting, user=user)
        url = settings.SITE_URL + _meeting_url(self.thesis, meeting)
        response = HttpResponse(
            ics.calendar(meeting, url=url, now=timezone.now()),
            content_type="text/calendar; charset=utf-8",
        )
        response["Content-Disposition"] = f'attachment; filename="meeting-{meeting.pk}.ics"'
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        # As the vault's downloads: nothing in the file may run if it is opened.
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        response["Referrer-Policy"] = "no-referrer"
        return response


# ------------------------------------------------------------- actions on one meeting


class _MeetingAction(_Mixin):
    """POST on one meeting: the service decides; errors re-render the detail page."""

    required_action = Action.MEETING_WRITE
    http_method_names: Sequence[str] = ("post",)
    operation = ""
    success = gettext_lazy("Saved.")

    def act(self, meeting: Meeting, user: User) -> None:
        raise NotImplementedError

    def post(
        self, request: HttpRequest, *args: Any, meeting_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        meeting = self._meeting(meeting_id, self.operation)
        self.kwargs_ = kwargs
        self.forms: dict[str, Any] = {}
        try:
            self.act(meeting, _user(request))
        except ReauthenticationRequiredError:  # the second factor expired meanwhile
            return reauth_response(request, mfa=True)
        except ValidationError as exc:
            return self._detail(
                meeting, status=_status(exc), error=" ".join(exc.messages), **self.forms
            )
        messages.success(request, str(self.success))
        return redirect(_meeting_url(self.thesis, meeting))

    def _valid(self, form: Any, key: str) -> dict[str, Any]:
        if not form.is_valid():
            self.forms[key] = form
            raise ValidationError([m for errors in form.errors.values() for m in errors])
        data: dict[str, Any] = form.cleaned_data
        return data


class CancelView(_MeetingAction, View):
    operation = "cancel"
    success = gettext_lazy("Meeting cancelled.")

    def act(self, meeting: Meeting, user: User) -> None:
        services.cancel_meeting(meeting=meeting, user=user)


class LevelView(_MeetingAction, _MeetingGuard, RecentAuthRequiredMixin, View):
    operation = "level"
    success = gettext_lazy("Confidentiality changed.")

    def act(self, meeting: Meeting, user: User) -> None:
        form = LevelForm(self.request.POST, user=user, thesis=self.thesis)
        data = self._valid(form, "level_form")
        services.change_level(
            meeting=meeting,
            user=user,
            level=ConfidentialityLevel(data["level"]),
            recent_mfa=authenticated_recently(self.request),
        )


class AgendaView(_MeetingAction, View):
    operation = "agenda"
    success = gettext_lazy("Agenda saved.")

    def act(self, meeting: Meeting, user: User) -> None:
        form = AgendaForm(self.request.POST)
        data = self._valid(form, "agenda_form")
        services.save_agenda(meeting=meeting, user=user, agenda=data["agenda"])


class PrefillView(_MeetingAction, View):
    operation = "prefill"
    success = gettext_lazy("Open actions and overdue objectives added to the agenda.")

    def act(self, meeting: Meeting, user: User) -> None:
        services.prefill_agenda(meeting=meeting, user=user)


class MinutesView(_MeetingAction, View):
    operation = "minutes"
    success = gettext_lazy("Minutes saved.")

    def act(self, meeting: Meeting, user: User) -> None:
        form = MinutesForm(self.request.POST)
        data = self._valid(form, "minutes_form")
        try:
            services.save_minutes(
                meeting=meeting, user=user, minutes=data["minutes"], version=data["version"]
            )
        except ValidationError:
            self.forms["minutes_form"] = form  # keep what was typed (stale: copy it)
            raise


class _Acknowledge(_MeetingAction):
    required_action = Action.MEETING_ACKNOWLEDGE
    operation = "acknowledge"
    kind = MinutesAcknowledgement.Kind.READ

    def act(self, meeting: Meeting, user: User) -> None:
        raw = self.request.POST.get("version", "")
        services.acknowledge(
            meeting=meeting,
            user=user,
            kind=self.kind,
            version=int(raw) if raw.isdigit() else -1,
            recent_mfa=authenticated_recently(self.request),
        )


class ReadView(_Acknowledge, View):
    success = gettext_lazy("Minutes marked as read.")


class ApproveView(_Acknowledge, _MeetingGuard, RecentAuthRequiredMixin, View):
    object_rule = "attendee"
    success = gettext_lazy("Minutes approved.")
    kind = MinutesAcknowledgement.Kind.APPROVED


class DecisionView(_MeetingAction, View):
    operation = "decision"
    success = gettext_lazy("Decision recorded.")

    def act(self, meeting: Meeting, user: User) -> None:
        form = DecisionForm(self.request.POST)
        data = self._valid(form, "decision_form")
        services.add_decision(meeting=meeting, user=user, text=data["text"])


class WithdrawDecisionView(_MeetingAction, View):
    operation = "withdraw_decision"
    success = gettext_lazy("Decision withdrawn.")

    def act(self, meeting: Meeting, user: User) -> None:
        decision = Decision.objects.filter(pk=self.kwargs_["decision_id"], meeting=meeting).first()
        if decision is None:
            raise services.refuse_unknown(user, meeting, self.operation, "decision")
        services.withdraw_decision(decision=decision, user=user)


class ActionView(_MeetingAction, View):
    operation = "action"
    success = gettext_lazy("Action recorded.")

    def act(self, meeting: Meeting, user: User) -> None:
        form = ActionForm(
            self.request.POST, thesis=self.thesis, objectives=_objectives(user, self.thesis)
        )
        data = self._valid(form, "action_form")
        services.add_action(
            meeting=meeting,
            user=user,
            title=data["title"],
            assignee=data["assignee"],
            due_date=data["due_date"],
            objective=data["objective"],
        )


class ActionStatusView(_MeetingAction, View):
    """Its assignee or a writer: MEETING_READ at the guard, the service decides."""

    required_action = Action.MEETING_READ
    operation = "action_status"
    success = gettext_lazy("Action updated.")

    def act(self, meeting: Meeting, user: User) -> None:
        action = ActionItem.objects.filter(pk=self.kwargs_["action_id"], meeting=meeting).first()
        if action is None:
            raise services.refuse_unknown(user, meeting, self.operation, "action")
        form = ActionStatusForm(self.request.POST)
        data = self._valid(form, "status_form")
        services.set_action_status(action=action, user=user, status=data["status"])


class DocumentView(_MeetingAction, View):
    operation = "link_document"
    success = gettext_lazy("Document linked.")

    def act(self, meeting: Meeting, user: User) -> None:
        form = DocumentForm(self.request.POST, documents=_documents(user, self.thesis))
        data = self._valid(form, "document_form")
        document = Document.objects.get(pk=data["document"], thesis=self.thesis)
        services.link_document(meeting=meeting, user=user, document=document)


class UnlinkDocumentView(_MeetingAction, View):
    operation = "unlink_document"
    success = gettext_lazy("Document unlinked.")

    def act(self, meeting: Meeting, user: User) -> None:
        link = MeetingDocument.objects.filter(pk=self.kwargs_["link_id"], meeting=meeting).first()
        if link is None:
            raise services.refuse_unknown(user, meeting, self.operation, "link")
        services.unlink_document(link=link, user=user)


# --------------------------------------------------------------------------- fragments


class ObjectiveActionsView(_Mixin, View):
    """« Linked actions » on an objective's page (htmx fragment, MEETING_READ)."""

    def get(
        self, request: HttpRequest, *args: Any, objective_id: Any, **kwargs: Any
    ) -> HttpResponse:
        user = _user(request)
        objective = Objective.objects.filter(pk=objective_id, thesis=self.thesis).first()
        if objective is None:
            raise PermissionDenied
        rows = services.actions_for_objective(user, objective)
        context = {
            "thesis": self.thesis,
            "rows": [(r.action, r.meeting, shown(r.title), r.overdue) for r in rows],
        }
        return render(request, "meetings/_objective_actions.html", context)
