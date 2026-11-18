"""Pages of the objectives (mounted under /t/<thesis_id>/objectives/).

Thin views. Order of checks, by class order (as in M3):
1. ThesisPermissionRequiredMixin (403, THESIS_ACCESS_DENIED audited);
2. RecentAuthRequiredMixin (second factor < 10 min) for approving one objective
   or a whole year, acknowledging changes, and changing the level: AFTER the
   guard (403 before any re-authentication) and BEFORE View;
3. the service, which decides again and audits its own refusals.

Errors: a state conflict (stale, frozen, already approved, nothing to
acknowledge, dropped) answers 409; invalid input 400; both render the page (or
the htmx fragment) with the message. URLs carry ids only, page titles are
generic: an objective's title never leaves the body of the page that shows it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import formats, timezone
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
from apps.vault import services as vault
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import Document

from . import charts, services
from .forms import CommentForm, DropForm, LevelForm, ObjectiveForm, ProgressForm
from .models import Category, Objective, Status, ThesisYear
from .stats import as_percent

CONFLICTS = frozenset({"stale", "frozen", "already_approved", "nothing_to_acknowledge", "dropped"})


def _user(request: HttpRequest) -> User:
    user = request.user
    if not isinstance(user, User):  # the thesis guard already refused anonymous users
        raise PermissionDenied
    return user


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _status(exc: ValidationError) -> int:
    return 409 if getattr(exc, "code", None) in CONFLICTS else 400


def month_label(day: dt.date) -> str:
    """« janv. 2026 »: Django's localized month abbreviation (format "N"), whose French
    translations for January and February are completed in our catalogue
    (apps/core/month_abbreviations.py)."""
    return formats.date_format(day, "N Y")


@dataclass(frozen=True)
class Shown:
    """A sealed text as the page shows it. Only readable objectives reach a page
    (SR-01): every text of an objective is at its level, so none is restricted."""

    text: str


def shown(value: Any) -> Shown:
    if value is vault.RESTRICTED:  # a bug upstream: refuse rather than hint
        raise PermissionDenied("a text of an unreadable objective reached a page")
    return Shown(text=str(value or ""))


def _shown_texts(texts: dict[str, Any]) -> dict[str, Shown]:
    return {name: shown(value) for name, value in texts.items()}


def _documents(user: User, thesis: Any) -> dict[str, Document]:
    """Documents of the thesis the user may read: the choices of evidence."""
    candidates = Document.objects.filter(thesis=thesis).order_by("title")
    return {str(d.pk): d for d in candidates if can_access_document(user, d)}


def _by_category(counts: dict[str, dict[str, int]]) -> list[tuple[str, str]]:
    """[(category label, "1 Planned, 1 Dropped")], in the order of the choices."""
    rows = []
    for category in Category:
        statuses = counts.get(category.value, {})
        if statuses:
            parts = [f"{statuses[s.value]} {s.label}" for s in Status if statuses.get(s.value)]
            rows.append((str(category.label), ", ".join(parts)))
    return rows


def _progress_form(objective: Objective, documents: dict[str, Document]) -> ProgressForm:
    """Pre-filled with the CURRENT status and progress: an untouched select must never
    send the first choice (« Prévu ») and set the objective back."""
    initial = {
        "status": objective.status,
        "progress": objective.progress,
        "date": timezone.localdate(),
    }
    return ProgressForm(documents=documents, initial=initial)


def _to_declare(objective: Objective) -> bool:
    """100 % but not declared achieved: the student decides (ACHIEVED is never automatic)."""
    return objective.status == Status.IN_PROGRESS and objective.progress == 100


class _Mixin(ThesisPermissionRequiredMixin):
    required_action = Action.OBJECTIVE_READ
    request: HttpRequest

    def _objective(self, objective_id: Any, operation: str) -> Objective:
        return services.objective_for(
            _user(self.request), self.thesis, objective_id, operation=operation
        )

    def _detail_url(self, objective: Objective) -> str:
        return reverse(
            "objectives:detail", kwargs={"thesis_id": self.thesis.pk, "objective_id": objective.pk}
        )

    def _detail(
        self,
        objective: Objective,
        *,
        status: int = 200,
        error: str = "",
        **forms: Any,
    ) -> HttpResponse:
        user = _user(self.request)
        fresh = Objective.objects.select_related("year", "approved_by").get(pk=objective.pk)
        detail = services.objective_detail(user, fresh)
        allowed = {
            action
            for action in (
                Action.OBJECTIVE_WRITE,
                Action.OBJECTIVE_COMMENT,
                Action.OBJECTIVE_COMMENT_READ,
                Action.VALIDATE_OBJECTIVES,
            )
            if can(user, action, self.thesis)
        }
        open_objective = fresh.status != Status.DROPPED
        writer = Action.OBJECTIVE_WRITE in allowed and open_objective
        context = {
            "thesis": self.thesis,
            "objective": fresh,
            "texts": _shown_texts(detail.texts),
            "revisions": [
                {
                    "revision": r.revision,
                    "reason": shown(r.reason),
                    "old": shown(r.old_value),
                    "new": shown(r.new_value),
                }
                for r in detail.revisions
            ],
            "progress": [
                {
                    "update": p.update,
                    "note": shown(p.note),
                    "evidence": p.evidence,
                }
                for p in detail.progress
            ],
            "can_write": writer,
            "can_comment": Action.OBJECTIVE_COMMENT in allowed,
            "can_read_comments": Action.OBJECTIVE_COMMENT_READ in allowed,
            "can_approve": Action.VALIDATE_OBJECTIVES in allowed
            and open_objective
            and not fresh.is_approved,
            "can_acknowledge": Action.VALIDATE_OBJECTIVES in allowed
            and (fresh.changed_since_approval or fresh.result_declared),
            "progress_form": forms.get("progress_form")
            or (_progress_form(fresh, _documents(user, self.thesis)) if writer else None),
            "to_declare": writer and _to_declare(fresh),
            "drop_form": forms.get("drop_form") or (DropForm() if writer else None),
            "level_form": forms.get("level_form")
            or (LevelForm(initial={"level": fresh.level}) if writer else None),
            "error": error,
        }
        return render(self.request, "objectives/detail.html", context, status=status)


# --------------------------------------------------------------------------- dashboard


class DashboardView(_Mixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        year = self._year(request.GET.get("year"))
        return self._render(year)

    def _year(self, raw: str | None) -> ThesisYear:
        years = ThesisYear.objects.filter(thesis=self.thesis)
        if raw and raw.isdigit():
            chosen = years.filter(number=int(raw)).first()
            if chosen is not None:
                return chosen
        current = services.current_year(self.thesis)
        fallback = years.order_by("number").first()
        if current is None and fallback is None:
            raise PermissionDenied  # a thesis always has years (post_save); never reached
        return current or fallback  # type: ignore[return-value]

    def _render(self, year: ThesisYear, *, status: int = 200, error: str = "") -> HttpResponse:
        user = _user(self.request)
        data = services.dashboard(user, self.thesis, year)
        series = [(month_label(point.month), point.value) for point in data.stats.monthly]
        context = {
            "thesis": self.thesis,
            "data": data,
            "stats": data.stats.stats,
            "attainment": as_percent(data.stats.stats.attainment),
            "progress_rate": as_percent(data.stats.stats.progress),
            "chart": charts.line_chart(series),
            "by_category": _by_category(data.stats.stats.by_category),
            "rows": [(r, _shown_texts(r.texts)) for r in data.objectives],
            "overdue": [(r, _shown_texts(r.texts)) for r in data.overdue],
            "pending": [(r, _shown_texts(r.texts)) for r in data.pending],
            "can_write": can(user, Action.OBJECTIVE_WRITE, self.thesis),
            "can_validate": can(user, Action.VALIDATE_OBJECTIVES, self.thesis),
            "error": error,
        }
        return render(self.request, "objectives/dashboard.html", context, status=status)


# --------------------------------------------------------------------------- create, edit


class NewObjectiveView(_Mixin, View):
    required_action = Action.OBJECTIVE_WRITE

    def _years(self) -> list[ThesisYear]:
        return list(ThesisYear.objects.filter(thesis=self.thesis).order_by("number"))

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        current = services.current_year(self.thesis)
        initial = {"year": current.number if current else 1}
        return self._page(ObjectiveForm(years=self._years(), initial=initial))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        years = self._years()
        form = ObjectiveForm(request.POST, years=years)
        if not form.is_valid():
            return self._page(form, status=400)
        data = form.cleaned_data
        try:
            objective = services.create_objective(
                thesis=self.thesis,
                user=_user(request),
                year=next(y for y in years if y.number == data["year"]),
                title=data["title"],
                description=data["description"],
                success_criterion=data["success_criterion"],
                category=data["category"],
                due_date=data["due_date"],
                weight=data["weight"],
                level=ConfidentialityLevel(data["level"]),
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
            return self._page(form, status=_status(exc))
        messages.success(request, _("Objective created."))
        return redirect(self._detail_url(objective))

    def _page(self, form: ObjectiveForm, status: int = 200) -> HttpResponse:
        context = {"thesis": self.thesis, "form": form, "objective": None}
        return render(self.request, "objectives/form.html", context, status=status)


class DetailView(_Mixin, View):
    def get(
        self, request: HttpRequest, *args: Any, objective_id: Any, **kwargs: Any
    ) -> HttpResponse:
        return self._detail(self._objective(objective_id, "detail"))


class EditView(_Mixin, View):
    required_action = Action.OBJECTIVE_WRITE

    def get(
        self, request: HttpRequest, *args: Any, objective_id: Any, **kwargs: Any
    ) -> HttpResponse:
        objective = self._objective(objective_id, "edit")
        texts = services.objective_texts(_user(request), self.thesis, [objective])[objective.pk]
        initial = {
            **{name: shown(value).text for name, value in texts.items()},
            "category": objective.category,
            "due_date": objective.due_date,
            "weight": objective.weight,
        }
        return self._page(objective, ObjectiveForm(initial=initial, approved=objective.is_approved))

    def post(
        self, request: HttpRequest, *args: Any, objective_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        objective = self._objective(objective_id, "edit")
        form = ObjectiveForm(request.POST, approved=objective.is_approved)
        if not form.is_valid():
            return self._page(objective, form, status=400)
        data = dict(form.cleaned_data)
        reason = data.pop("reason", "")
        try:
            services.update_objective(
                objective=objective, user=_user(request), changes=data, reason=reason
            )
        except ValidationError as exc:
            form.add_error("reason" if exc.code == "reason_required" else None, exc.messages)
            return self._page(objective, form, status=_status(exc))
        messages.success(request, _("Objective saved."))
        return redirect(self._detail_url(objective))

    def _page(self, objective: Objective, form: ObjectiveForm, status: int = 200) -> HttpResponse:
        context = {"thesis": self.thesis, "form": form, "objective": objective}
        return render(self.request, "objectives/form.html", context, status=status)


# ------------------------------------------------------------ actions on one objective


class _ObjectiveAction(_Mixin):
    """POST on one objective: the service decides; errors re-render the detail page."""

    http_method_names: Sequence[str] = ("post",)
    operation = ""
    success = gettext_lazy("Saved.")

    def act(self, objective: Objective, user: User) -> None:
        raise NotImplementedError

    def post(
        self, request: HttpRequest, *args: Any, objective_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        objective = self._objective(objective_id, self.operation)
        self.forms: dict[str, Any] = {}
        try:
            self.act(objective, _user(request))
        except ReauthenticationRequiredError:  # the second factor expired meanwhile
            return reauth_response(request, mfa=True)
        except ValidationError as exc:
            return self._detail(
                objective, status=_status(exc), error=" ".join(exc.messages), **self.forms
            )
        messages.success(request, str(self.success))
        return redirect(self._detail_url(objective))

    def _valid(self, form: Any, key: str) -> dict[str, Any]:
        if not form.is_valid():
            self.forms[key] = form
            raise ValidationError([m for errors in form.errors.values() for m in errors])
        data: dict[str, Any] = form.cleaned_data
        return data


class DropView(_ObjectiveAction, View):
    required_action = Action.OBJECTIVE_WRITE
    operation = "drop"
    success = gettext_lazy("Objective dropped.")

    def act(self, objective: Objective, user: User) -> None:
        form = DropForm(self.request.POST)
        data = self._valid(form, "drop_form")
        try:
            services.drop_objective(objective=objective, user=user, reason=data["reason"])
        except ValidationError:
            self.forms["drop_form"] = form
            raise


class ApproveView(_ObjectiveAction, RecentAuthRequiredMixin, View):
    required_action = Action.VALIDATE_OBJECTIVES
    operation = "approve"
    success = gettext_lazy("Objective approved.")

    def act(self, objective: Objective, user: User) -> None:
        services.approve_objective(
            objective=objective, user=user, recent_mfa=authenticated_recently(self.request)
        )


class AcknowledgeView(_ObjectiveAction, RecentAuthRequiredMixin, View):
    required_action = Action.VALIDATE_OBJECTIVES
    operation = "acknowledge"
    success = gettext_lazy("Changes acknowledged.")

    def act(self, objective: Objective, user: User) -> None:
        services.acknowledge_changes(
            objective=objective, user=user, recent_mfa=authenticated_recently(self.request)
        )


class LevelView(_ObjectiveAction, RecentAuthRequiredMixin, View):
    required_action = Action.OBJECTIVE_WRITE
    operation = "level"
    success = gettext_lazy("Confidentiality changed.")

    def act(self, objective: Objective, user: User) -> None:
        form = LevelForm(self.request.POST)
        data = self._valid(form, "level_form")
        level = ConfidentialityLevel(data["level"])
        try:
            services.update_objective(
                objective=objective, user=user, changes={"level": level}, reason=data["reason"]
            )
        except ValidationError:
            self.forms["level_form"] = form
            raise


class ApproveYearView(_Mixin, RecentAuthRequiredMixin, View):
    required_action = Action.VALIDATE_OBJECTIVES
    http_method_names: Sequence[str] = ("post",)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        raw = request.POST.get("year", "")
        year = (
            ThesisYear.objects.filter(thesis=self.thesis, number=int(raw)).first()
            if raw.isdigit()
            else None
        )
        dashboard = DashboardView()
        dashboard.request, dashboard.thesis = request, self.thesis
        if year is None:
            fallback = dashboard._year(None)
            return dashboard._render(fallback, status=400, error=_("Unknown year."))
        try:
            count = services.approve_year(
                year=year, user=_user(request), recent_mfa=authenticated_recently(request)
            )
        except ReauthenticationRequiredError:
            return reauth_response(request, mfa=True)
        except ValidationError as exc:
            return dashboard._render(year, status=_status(exc), error=" ".join(exc.messages))
        messages.success(request, _("%(count)d objective(s) approved.") % {"count": count})
        url = reverse("objectives:dashboard", kwargs={"thesis_id": self.thesis.pk})
        return redirect(f"{url}?year={year.number}")


# --------------------------------------------------------------------------- htmx fragments


class ProgressView(_Mixin, View):
    """POST a progress report; htmx gets the progress section back (also on 400/409)."""

    required_action = Action.OBJECTIVE_WRITE
    http_method_names: Sequence[str] = ("post",)

    def post(
        self, request: HttpRequest, *args: Any, objective_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        user = _user(request)
        objective = self._objective(objective_id, "progress")
        documents = _documents(user, self.thesis)
        form = ProgressForm(request.POST, documents=documents)
        status, error = 200, ""
        if form.is_valid():
            data = form.cleaned_data
            try:
                services.add_progress(
                    objective=objective,
                    user=user,
                    date=data["date"],
                    progress=data["progress"],
                    status=data["status"],
                    note=data["note"],
                    evidence=data["evidence"],
                )
                form = _progress_form(Objective.objects.get(pk=objective.pk), documents)
            except ValidationError as exc:
                status, error = _status(exc), " ".join(exc.messages)
        else:
            status = 400
        if not _is_htmx(request):
            if status == 200:
                messages.success(request, _("Progress recorded."))
                return redirect(self._detail_url(objective))
            return self._detail(objective, status=status, error=error, progress_form=form)
        detail = services.objective_detail(user, objective)
        context = {
            "thesis": self.thesis,
            "objective": Objective.objects.get(pk=objective.pk),
            "progress": [
                {
                    "update": p.update,
                    "note": shown(p.note),
                    "evidence": p.evidence,
                }
                for p in detail.progress
            ],
            "progress_form": form,
            "can_write": True,
            "error": error,
        }
        return render(request, "objectives/_progress.html", context, status=status)


class CommentsView(_Mixin, View):
    """The comment thread: read with OBJECTIVE_COMMENT_READ (the CSI member gets 403);
    writing a comment needs OBJECTIVE_COMMENT, checked again by add_comment (M13a:
    a closed thesis keeps the thread readable, never writable)."""

    required_action = Action.OBJECTIVE_COMMENT_READ

    def get(
        self, request: HttpRequest, *args: Any, objective_id: Any, **kwargs: Any
    ) -> HttpResponse:
        objective = self._objective(objective_id, "comments")
        return self._fragment(objective, CommentForm())

    def post(
        self, request: HttpRequest, *args: Any, objective_id: Any, **kwargs: Any
    ) -> HttpResponse:
        objective = self._objective(objective_id, "comment")
        form = CommentForm(request.POST)
        if not form.is_valid():
            return self._fragment(objective, form, status=400)
        try:
            services.add_comment(
                objective=objective, user=_user(request), body=form.cleaned_data["body"]
            )
        except ValidationError as exc:
            return self._fragment(
                objective, form, status=_status(exc), error=" ".join(exc.messages)
            )
        return self._fragment(objective, CommentForm())

    def _fragment(
        self, objective: Objective, form: CommentForm, *, status: int = 200, error: str = ""
    ) -> HttpResponse:
        comments = services.comments_for(_user(self.request), objective)
        context = {
            "thesis": self.thesis,
            "objective": objective,
            "comments": [(c.comment, shown(c.body)) for c in comments],
            "form": form
            if can(_user(self.request), Action.OBJECTIVE_COMMENT, self.thesis)
            else None,
            "error": error,
        }
        return render(self.request, "objectives/_comments.html", context, status=status)
