"""Pages of the portfolio (M9a), mounted under /t/<thesis_id>/portfolio/.

Thin views. Order of checks, by class order (as in M4):
1. ThesisPermissionRequiredMixin (403, THESIS_ACCESS_DENIED audited);
2. RecentAuthRequiredMixin (second factor < 10 min) for changing the level and
   downloading the CSV: AFTER the guard (403 before any re-authentication);
3. the service, which decides again and audits its own refusals.

A hidden training answers as an unknown id (the same audited 403). Errors: a
state conflict (stale, frozen) answers 409, invalid input 400. URLs carry ids
only, page titles are generic: a training's title never leaves the body of the
page that shows it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.models import User
from apps.accounts.permissions import Action, ThesisPermissionRequiredMixin, can
from apps.accounts.reauth import RecentAuthRequiredMixin, authenticated_recently, reauth_response
from apps.core.confidentiality import ConfidentialityLevel
from apps.vault.errors import ReauthenticationRequiredError

from . import services
from .forms import LevelForm, SettingsForm, TrainingForm
from .models import AdumStatus, Training

CONFLICTS = frozenset({"stale", "frozen"})


def _user(request: HttpRequest) -> User:
    user = request.user
    if not isinstance(user, User):  # the thesis guard already refused anonymous users
        raise PermissionDenied
    return user


def _status(exc: ValidationError) -> int:
    return 409 if getattr(exc, "code", None) in CONFLICTS else 400


def _error(exc: ValidationError) -> str:
    return " ".join(exc.messages)


class _Mixin(ThesisPermissionRequiredMixin):
    required_action = Action.TRAINING_READ
    request: HttpRequest

    def _training(self, training_id: Any, operation: str) -> Training:
        return services.training_for(
            _user(self.request), self.thesis, training_id, operation=operation
        )

    def _detail_url(self, training: Training) -> str:
        return reverse(
            "portfolio:detail", kwargs={"thesis_id": self.thesis.pk, "training_id": training.pk}
        )


class TrainingsView(_Mixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        listing = services.training_list(_user(request), self.thesis)
        return render(
            request, "portfolio/trainings.html", {"thesis": self.thesis, "listing": listing}
        )


class NewTrainingView(_Mixin, View):
    required_action = Action.TRAINING_WRITE

    def _form(self, data: Any = None) -> TrainingForm:
        documents = services.attachable_documents(_user(self.request), self.thesis)
        return TrainingForm(data, documents=documents)

    def _render(self, form: TrainingForm, *, status: int = 200, error: str = "") -> HttpResponse:
        context = {"thesis": self.thesis, "form": form, "training": None, "error": error}
        return render(self.request, "portfolio/form.html", context, status=status)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._render(self._form())

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        form = self._form(request.POST)
        if not form.is_valid():
            return self._render(form, status=400)
        try:
            training = services.create_training(
                thesis=self.thesis,
                user=_user(request),
                level=ConfidentialityLevel(form.cleaned_data["level"]),
                **form.values(),
            )
        except ValidationError as exc:
            return self._render(form, status=_status(exc), error=_error(exc))
        messages.success(request, _("Training added."))
        return redirect(self._detail_url(training))


class DetailView(_Mixin, View):
    def _render(
        self, training: Training, *, status: int = 200, error: str = "", level_form: Any = None
    ) -> HttpResponse:
        user = _user(self.request)
        row = services.training_detail(user, training)
        can_write = can(user, Action.TRAINING_WRITE, self.thesis)
        context = {
            "thesis": self.thesis,
            "row": row,
            "training": training,
            "can_write": can_write,
            "level_form": level_form or LevelForm(initial={"level": training.level}),
            "entered": training.adum_status == AdumStatus.ENTERED,
            "error": error,
        }
        return render(self.request, "portfolio/detail.html", context, status=status)

    def get(
        self, request: HttpRequest, *args: Any, training_id: Any, **kwargs: Any
    ) -> HttpResponse:
        return self._render(self._training(training_id, "detail"))


class EditView(_Mixin, View):
    required_action = Action.TRAINING_WRITE

    def _form(self, training: Training, data: Any = None) -> TrainingForm:
        user = _user(self.request)
        documents = services.attachable_documents(user, self.thesis)
        row = services.training_detail(user, training)
        initial = {
            "title": row.title,
            "provider": row.provider,
            "notes": row.notes,
            "category": training.category,
            "hours": str(training.hours.normalize()).replace(".", ","),
            "start_date": training.start_date,
            "end_date": training.end_date,
            "certificate": str(training.certificate_id or ""),
        }
        return TrainingForm(data, documents=documents, with_level=False, initial=initial)

    def _render(
        self, training: Training, form: TrainingForm, *, status: int = 200, error: str = ""
    ) -> HttpResponse:
        context = {"thesis": self.thesis, "form": form, "training": training, "error": error}
        return render(self.request, "portfolio/form.html", context, status=status)

    def get(
        self, request: HttpRequest, *args: Any, training_id: Any, **kwargs: Any
    ) -> HttpResponse:
        training = self._training(training_id, "edit")
        return self._render(training, self._form(training))

    def post(
        self, request: HttpRequest, *args: Any, training_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        training = self._training(training_id, "edit")
        form = self._form(training, request.POST)
        if not form.is_valid():
            return self._render(training, form, status=400)
        try:
            services.update_training(training=training, user=_user(request), values=form.values())
        except ValidationError as exc:
            return self._render(training, form, status=_status(exc), error=_error(exc))
        messages.success(request, _("Training saved."))
        return redirect(self._detail_url(training))


class LevelView(_Mixin, RecentAuthRequiredMixin, View):
    required_action = Action.TRAINING_WRITE
    http_method_names: Sequence[str] = ("post",)

    def post(
        self, request: HttpRequest, *args: Any, training_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        training = self._training(training_id, "level")
        form = LevelForm(request.POST)
        detail = DetailView()
        detail.request, detail.thesis = request, self.thesis
        if not form.is_valid():
            return detail._render(training, status=400, level_form=form)
        try:
            services.change_training_level(
                training=training,
                user=_user(request),
                level=ConfidentialityLevel(form.cleaned_data["level"]),
                recent_mfa=authenticated_recently(request),
            )
        except ReauthenticationRequiredError:
            return reauth_response(request, mfa=True)
        except ValidationError as exc:
            return detail._render(training, status=_status(exc), error=_error(exc))
        messages.success(request, _("Confidentiality changed."))
        return redirect(self._detail_url(training))


class AdumStatusView(_Mixin, View):
    required_action = Action.TRAINING_WRITE
    http_method_names: Sequence[str] = ("post",)

    def post(
        self, request: HttpRequest, *args: Any, training_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        training = self._training(training_id, "adum")
        status = request.POST.get("status", "")
        try:
            services.set_adum_status(training=training, user=_user(request), status=status)
        except ValidationError as exc:
            detail = DetailView()
            detail.request, detail.thesis = request, self.thesis
            return detail._render(training, status=_status(exc), error=_error(exc))
        return redirect(self._detail_url(training))


def _year_param(request: HttpRequest) -> int | None:
    raw = request.GET.get("year", request.POST.get("year", ""))
    return int(raw) if raw.isdigit() else None


def _internal_param(request: HttpRequest) -> bool:
    return (request.GET.get("internal") or request.POST.get("internal")) == "1"


class AdumView(_Mixin, View):
    required_action = Action.PORTFOLIO_EXPORT

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            summary = services.adum_summary(
                _user(request),
                self.thesis,
                year=_year_param(request),
                include_internal=_internal_param(request),
            )
        except ValidationError as exc:
            failed: dict[str, Any] = {"thesis": self.thesis, "summary": None, "error": _error(exc)}
            return render(request, "portfolio/adum.html", failed, status=400)
        context: dict[str, Any] = {"thesis": self.thesis, "summary": summary, "error": ""}
        return render(request, "portfolio/adum.html", context)


class AdumCsvView(_Mixin, RecentAuthRequiredMixin, View):
    required_action = Action.PORTFOLIO_EXPORT
    http_method_names: Sequence[str] = ("post",)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        try:
            name, content = services.adum_csv(
                thesis=self.thesis,
                user=_user(request),
                year=_year_param(request),
                include_internal=_internal_param(request),
                recent_mfa=authenticated_recently(request),
            )
        except ReauthenticationRequiredError:
            return reauth_response(request, mfa=True)
        except ValidationError as exc:
            context = {"thesis": self.thesis, "summary": None, "error": _error(exc)}
            return render(request, "portfolio/adum.html", context, status=400)
        response = HttpResponse(content, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "no-store"
        return response


class SettingsView(_Mixin, View):
    required_action = Action.TRAINING_WRITE

    def _render(self, form: SettingsForm, *, status: int = 200, error: str = "") -> HttpResponse:
        context = {"thesis": self.thesis, "form": form, "error": error}
        return render(self.request, "portfolio/settings.html", context, status=status)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        policy = services.policy_of(self.thesis)
        return self._render(
            SettingsForm(initial={"reregistration_date": policy.reregistration_date})
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        form = SettingsForm(request.POST)
        if not form.is_valid():
            return self._render(form, status=400)
        try:
            services.set_reregistration_date(
                thesis=self.thesis,
                user=_user(request),
                day=form.cleaned_data["reregistration_date"],
            )
        except ValidationError as exc:
            return self._render(form, status=_status(exc), error=_error(exc))
        messages.success(request, _("Settings saved."))
        return redirect("portfolio:settings", thesis_id=self.thesis.pk)
