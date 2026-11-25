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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views import View

from apps.accounts.models import User
from apps.accounts.permissions import Action, ThesisPermissionRequiredMixin, can
from apps.accounts.reauth import RecentAuthRequiredMixin, authenticated_recently, reauth_response
from apps.core.confidentiality import ConfidentialityLevel
from apps.vault.errors import ReauthenticationRequiredError

from . import services
from .forms import (
    CareerForm,
    LevelForm,
    OutreachForm,
    PublicationDetailsForm,
    SettingsForm,
    SkillForm,
    TeachingForm,
    TrainingForm,
)
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


def _accepted_param(request: HttpRequest) -> bool:
    return (request.GET.get("accepted") or request.POST.get("accepted")) == "1"


def _section_param(request: HttpRequest) -> str:
    return request.POST.get("section", "trainings")


class AdumView(_Mixin, View):
    required_action = Action.PORTFOLIO_EXPORT

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            summary = services.adum_summary(
                _user(request),
                self.thesis,
                year=_year_param(request),
                include_internal=_internal_param(request),
                include_accepted=_accepted_param(request),
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
                section=_section_param(request),
                include_accepted=_accepted_param(request),
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


# =========================================================================== M9b


@dataclass(frozen=True)
class _Kind:
    """What the generic pages of teaching and outreach need of each one."""

    key: str
    kwarg: str
    list_name: str
    detail: str
    edit: str
    level: str
    new: str
    read: Action
    write: Action
    form: Any
    title: Any
    one: Any
    new_label: Any
    main: str
    second: str
    mention: Any
    empty: Any
    added: Any
    get: Callable[..., Any]
    detail_of: Callable[..., Any]
    listing: Callable[..., Any]
    create: Callable[..., Any]
    update: Callable[..., Any]
    change: Callable[..., Any]
    with_hours: bool


TEACHING_PAGES = _Kind(
    key="teaching", kwarg="teaching_id", list_name="teachings", detail="teaching",
    edit="edit_teaching", level="teaching_level", new="new_teaching",
    read=Action.TEACHING_READ, write=Action.TEACHING_WRITE, form=TeachingForm,
    title=gettext_lazy("Teaching"), one=gettext_lazy("Teaching"),
    new_label=gettext_lazy("Add a teaching"), main="course", second="institution",
    mention=gettext_lazy("Computed on the teaching you can read."),
    empty=gettext_lazy("No teaching yet."), added=gettext_lazy("Teaching added."),
    get=services.teaching_for, detail_of=services.teaching_detail,
    listing=services.teaching_list, create=services.create_teaching,
    update=lambda obj, user, values: services.update_teaching(teaching=obj, user=user,
                                                              values=values),
    change=lambda obj, user, level, mfa: services.change_teaching_level(
        teaching=obj, user=user, level=level, recent_mfa=mfa),
    with_hours=True,
)  # fmt: skip
OUTREACH_PAGES = _Kind(
    key="outreach", kwarg="outreach_id", list_name="outreach_list", detail="outreach",
    edit="edit_outreach", level="outreach_level", new="new_outreach",
    read=Action.OUTREACH_READ, write=Action.OUTREACH_WRITE, form=OutreachForm,
    title=gettext_lazy("Outreach"), one=gettext_lazy("Outreach action"),
    new_label=gettext_lazy("Add an outreach action"), main="title", second="audience",
    mention=gettext_lazy("Computed on the outreach actions you can read."),
    empty=gettext_lazy("No outreach action yet."), added=gettext_lazy("Outreach action added."),
    get=services.outreach_for, detail_of=services.outreach_detail,
    listing=services.outreach_list, create=services.create_outreach,
    update=lambda obj, user, values: services.update_outreach(outreach=obj, user=user,
                                                              values=values),
    change=lambda obj, user, level, mfa: services.change_outreach_level(
        outreach=obj, user=user, level=level, recent_mfa=mfa),
    with_hours=False,
)  # fmt: skip


class _ActivityMixin(ThesisPermissionRequiredMixin):
    pages: _Kind
    request: HttpRequest

    def _url(self, name: str, obj: Any = None) -> str:
        kwargs: dict[str, Any] = {"thesis_id": self.thesis.pk}
        if obj is not None:
            kwargs[self.pages.kwarg] = obj.pk
        return reverse(f"portfolio:{name}", kwargs=kwargs)

    def _obj(self, kwargs: dict[str, Any], operation: str) -> Any:
        return self.pages.get(
            _user(self.request), self.thesis, kwargs[self.pages.kwarg], operation=operation
        )

    def _context(self, **extra: Any) -> dict[str, Any]:
        return {"thesis": self.thesis, "pages": self.pages, **extra}


class ActivitiesView(_ActivityMixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        listing = self.pages.listing(_user(request), self.thesis)
        context = self._context(
            listing=listing,
            done=[(r, self._url(self.pages.detail, r.obj)) for r in listing.done],
            upcoming=[(r, self._url(self.pages.detail, r.obj)) for r in listing.upcoming],
            new_url=self._url(self.pages.new),
        )
        return render(request, "portfolio/activities.html", context)


class NewActivityView(_ActivityMixin, View):
    def _render(self, form: Any, *, status: int = 200, error: str = "") -> HttpResponse:
        context = self._context(form=form, back_url=self._url(self.pages.list_name),
                                heading=self.pages.new_label, error=error)  # fmt: skip
        return render(self.request, "portfolio/edit.html", context, status=status)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._render(self.pages.form())

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        form = self.pages.form(request.POST)
        if not form.is_valid():
            return self._render(form, status=400)
        try:
            obj = self.pages.create(
                thesis=self.thesis, user=_user(request),
                level=ConfidentialityLevel(form.cleaned_data["level"]), **form.values(),
            )  # fmt: skip
        except ValidationError as exc:
            return self._render(form, status=_status(exc), error=_error(exc))
        messages.success(request, self.pages.added)
        return redirect(self._url(self.pages.detail, obj))


class ActivityView(_ActivityMixin, View):
    def _render(
        self, obj: Any, *, status: int = 200, error: str = "", level_form: Any = None
    ) -> HttpResponse:
        user = _user(self.request)
        context = self._context(
            row=self.pages.detail_of(user, obj), obj=obj, error=error,
            can_write=can(user, self.pages.write, self.thesis),
            level_form=level_form or LevelForm(initial={"level": obj.level}),
            back_url=self._url(self.pages.list_name), edit_url=self._url(self.pages.edit, obj),
            level_url=self._url(self.pages.level, obj),
        )  # fmt: skip
        return render(self.request, "portfolio/activity.html", context, status=status)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._render(self._obj(kwargs, "detail"))


class EditActivityView(_ActivityMixin, View):
    def _form(self, obj: Any, data: Any = None) -> Any:
        row = self.pages.detail_of(_user(self.request), obj)
        initial = dict(row.texts)
        initial["kind"] = obj.kind
        if self.pages.with_hours:
            initial |= {"hours": str(obj.hours.normalize()).replace(".", ","),
                        "start_date": obj.start_date, "end_date": obj.end_date}  # fmt: skip
        else:
            initial["date"] = obj.date
        return self.pages.form(data, with_level=False, initial=initial)

    def _render(self, obj: Any, form: Any, *, status: int = 200, error: str = "") -> HttpResponse:
        context = self._context(form=form, back_url=self._url(self.pages.detail, obj),
                                heading=_("Edit"), error=error)  # fmt: skip
        return render(self.request, "portfolio/edit.html", context, status=status)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        obj = self._obj(kwargs, "edit")
        return self._render(obj, self._form(obj))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        obj = self._obj(kwargs, "edit")
        form = self._form(obj, request.POST)
        if not form.is_valid():
            return self._render(obj, form, status=400)
        try:
            self.pages.update(obj, _user(request), form.values())
        except ValidationError as exc:
            return self._render(obj, form, status=_status(exc), error=_error(exc))
        messages.success(request, _("Saved."))
        return redirect(self._url(self.pages.detail, obj))


class ActivityLevelView(_ActivityMixin, RecentAuthRequiredMixin, View):
    http_method_names: Sequence[str] = ("post",)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        obj = self._obj(kwargs, "level")
        form = LevelForm(request.POST)
        detail = ActivityView()
        detail.request, detail.thesis = request, self.thesis
        detail.pages = self.pages
        if not form.is_valid():
            return detail._render(obj, status=400, level_form=form)
        try:
            self.pages.change(obj, _user(request), ConfidentialityLevel(form.cleaned_data["level"]),
                              authenticated_recently(request))  # fmt: skip
        except ReauthenticationRequiredError:
            return reauth_response(request, mfa=True)
        except ValidationError as exc:
            return detail._render(obj, status=_status(exc), error=_error(exc))
        messages.success(request, _("Confidentiality changed."))
        return redirect(self._url(self.pages.detail, obj))


# Explicit classes, not made by type(): the guards are read in the source (Caddy test).
class TeachingsView(ActivitiesView):
    pages = TEACHING_PAGES
    required_action = Action.TEACHING_READ


class NewTeachingView(NewActivityView):
    pages = TEACHING_PAGES
    required_action = Action.TEACHING_WRITE


class TeachingView(ActivityView):
    pages = TEACHING_PAGES
    required_action = Action.TEACHING_READ


class EditTeachingView(EditActivityView):
    pages = TEACHING_PAGES
    required_action = Action.TEACHING_WRITE


class TeachingLevelView(ActivityLevelView):
    pages = TEACHING_PAGES
    required_action = Action.TEACHING_WRITE


class OutreachListView(ActivitiesView):
    pages = OUTREACH_PAGES
    required_action = Action.OUTREACH_READ


class NewOutreachView(NewActivityView):
    pages = OUTREACH_PAGES
    required_action = Action.OUTREACH_WRITE


class OutreachView(ActivityView):
    pages = OUTREACH_PAGES
    required_action = Action.OUTREACH_READ


class EditOutreachView(EditActivityView):
    pages = OUTREACH_PAGES
    required_action = Action.OUTREACH_WRITE


class OutreachLevelView(ActivityLevelView):
    pages = OUTREACH_PAGES
    required_action = Action.OUTREACH_WRITE


# --------------------------------------------------------------------------- publications


class _PublicationMixin(ThesisPermissionRequiredMixin):
    required_action = Action.SUBMISSION_READ
    request: HttpRequest

    def _submission(self, submission_id: Any, operation: str) -> Any:
        return services.publication_for(
            _user(self.request), self.thesis, submission_id, operation=operation
        )

    def _url(self, name: str, submission: Any = None) -> str:
        kwargs: dict[str, Any] = {"thesis_id": self.thesis.pk}
        if submission is not None:
            kwargs["submission_id"] = submission.pk
        return reverse(f"portfolio:{name}", kwargs=kwargs)


class PublicationsView(_PublicationMixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        rows = services.publication_list(_user(request), self.thesis)
        context = {
            "thesis": self.thesis,
            "rows": [(r, self._url("publication", r.submission)) for r in rows],
        }
        return render(request, "portfolio/publications.html", context)


class PublicationView(_PublicationMixin, View):
    def get(
        self, request: HttpRequest, *args: Any, submission_id: Any, **kwargs: Any
    ) -> HttpResponse:
        user = _user(request)
        submission = self._submission(submission_id, "publication")
        context = {
            "thesis": self.thesis,
            "row": services.publication_detail(user, submission),
            "can_write": can(user, Action.SUBMISSION_WRITE, self.thesis),
            "edit_url": self._url("edit_publication", submission),
            "back_url": self._url("publications"),
            "submission_url": reverse(
                "conferences:submission",
                kwargs={"thesis_id": self.thesis.pk, "submission_id": submission.pk},
            ),
        }
        return render(request, "portfolio/publication.html", context)


class EditPublicationView(_PublicationMixin, View):
    required_action = Action.SUBMISSION_WRITE

    def _form(self, submission: Any, data: Any = None) -> PublicationDetailsForm:
        row = services.publication_detail(_user(self.request), submission)
        initial: dict[str, Any] = {**row.texts, "adum_category": row.category}
        if row.details is not None:
            initial["presented_on"] = row.details.presented_on
        return PublicationDetailsForm(data, initial=initial)

    def _render(
        self, submission: Any, form: Any, *, status: int = 200, error: str = ""
    ) -> HttpResponse:
        context = {"thesis": self.thesis, "form": form, "error": error,
                   "back_url": self._url("publication", submission),
                   "heading": _("Publication details")}  # fmt: skip
        return render(self.request, "portfolio/edit.html", context, status=status)

    def get(
        self, request: HttpRequest, *args: Any, submission_id: Any, **kwargs: Any
    ) -> HttpResponse:
        submission = self._submission(submission_id, "edit_publication")
        return self._render(submission, self._form(submission))

    def post(
        self, request: HttpRequest, *args: Any, submission_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        submission = self._submission(submission_id, "edit_publication")
        form = self._form(submission, request.POST)
        if not form.is_valid():
            return self._render(submission, form, status=400)
        try:
            services.set_publication_details(
                submission=submission, user=_user(request), values=form.values()
            )
        except ValidationError as exc:
            return self._render(submission, form, status=_status(exc), error=_error(exc))
        messages.success(request, _("Saved."))
        return redirect(self._url("publication", submission))


# --------------------------------------------------------------------------- the student's notes


class _PersonalMixin(ThesisPermissionRequiredMixin):
    required_action = Action.CAREER_NOTE_WRITE
    request: HttpRequest

    def _skills_url(self) -> str:
        return reverse("portfolio:skills", kwargs={"thesis_id": self.thesis.pk})


class SkillsView(_PersonalMixin, View):
    required_action = Action.CAREER_NOTE_READ

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = _user(request)
        rows = services.skill_list(user, self.thesis)
        context = {
            "thesis": self.thesis,
            "rows": [
                (
                    r,
                    reverse(
                        "portfolio:edit_skill",
                        kwargs={"thesis_id": self.thesis.pk, "skill_id": r.skill.pk},
                    ),
                )
                for r in rows
            ],
            "career": services.career_note(user, self.thesis),
            "can_write": can(user, Action.CAREER_NOTE_WRITE, self.thesis),
        }
        return render(request, "portfolio/skills.html", context)


class _SkillFormView(_PersonalMixin, View):
    def _render(self, form: Any, *, status: int = 200, error: str = "") -> HttpResponse:
        context = {"thesis": self.thesis, "form": form, "error": error,
                   "back_url": self._skills_url(), "heading": _("Skill"),
                   "intro": _("Read by you only.")}  # fmt: skip
        return render(self.request, "portfolio/edit.html", context, status=status)


class NewSkillView(_SkillFormView):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._render(SkillForm())

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        form = SkillForm(request.POST)
        if not form.is_valid():
            return self._render(form, status=400)
        try:
            services.create_skill(thesis=self.thesis, user=_user(request), **form.values())
        except ValidationError as exc:
            return self._render(form, status=_status(exc), error=_error(exc))
        messages.success(request, _("Saved."))
        return redirect(self._skills_url())


class EditSkillView(_SkillFormView):
    def _skill(self, skill_id: Any) -> Any:
        return services.skill_for(_user(self.request), self.thesis, skill_id, operation="edit")

    def _form(self, skill: Any, data: Any = None) -> SkillForm:
        row = services.skill_detail(_user(self.request), skill)
        return SkillForm(data, initial={**row.texts, "category": skill.category})

    def get(self, request: HttpRequest, *args: Any, skill_id: Any, **kwargs: Any) -> HttpResponse:
        skill = self._skill(skill_id)
        return self._render(self._form(skill))

    def post(
        self, request: HttpRequest, *args: Any, skill_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        skill = self._skill(skill_id)
        form = self._form(skill, request.POST)
        if not form.is_valid():
            return self._render(form, status=400)
        try:
            services.update_skill(skill=skill, user=_user(request), values=form.values())
        except ValidationError as exc:
            return self._render(form, status=_status(exc), error=_error(exc))
        messages.success(request, _("Saved."))
        return redirect(self._skills_url())


class CareerView(_PersonalMixin, View):
    def _render(self, form: Any, *, status: int = 200, error: str = "") -> HttpResponse:
        context = {"thesis": self.thesis, "form": form, "error": error,
                   "back_url": self._skills_url(), "heading": _("Professional project"),
                   "intro": _("Read by you only.")}  # fmt: skip
        return render(self.request, "portfolio/edit.html", context, status=status)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        text = services.career_note(_user(request), self.thesis)
        return self._render(CareerForm(initial={"text": text}))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        form = CareerForm(request.POST)
        if not form.is_valid():
            return self._render(form, status=400)
        try:
            services.set_career_note(
                thesis=self.thesis, user=_user(request), text=form.cleaned_data["text"]
            )
        except ValidationError as exc:
            return self._render(form, status=_status(exc), error=_error(exc))
        messages.success(request, _("Saved."))
        return redirect(self._skills_url())


class AdumBibtexView(_Mixin, RecentAuthRequiredMixin, View):
    required_action = Action.PORTFOLIO_EXPORT
    http_method_names: Sequence[str] = ("post",)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        try:
            name, content = services.publications_bibtex(
                thesis=self.thesis,
                user=_user(request),
                include_internal=_internal_param(request),
                include_accepted=_accepted_param(request),
                recent_mfa=authenticated_recently(request),
            )
        except ReauthenticationRequiredError:
            return reauth_response(request, mfa=True)
        response = HttpResponse(content, content_type="application/x-bibtex; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "no-store"
        return response
