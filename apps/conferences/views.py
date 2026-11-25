"""Pages of the conferences (mounted under /t/<thesis_id>/conferences/).

Thin views. Order of checks, by class order (as in M3, M4, M5a):
1. ThesisPermissionRequiredMixin (403, THESIS_ACCESS_DENIED audited);
2. RecentAuthRequiredMixin (second factor < 10 min) for deciding an ST approval,
   turning st_work off, setting the review period (the tutor) and changing the
   level (the student): AFTER the guard, BEFORE View;
3. the service, which decides again and audits its own refusals.

Errors: a state conflict answers 409, invalid input 400; both render the page
with the message. URLs carry ids only; a submission's title never leaves the
body of the pages that show it.
"""

from __future__ import annotations

from collections.abc import Sequence
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
from apps.accounts.permissions import (
    Action,
    ThesisPermissionRequiredMixin,
    can,
    can_access_document,
)
from apps.accounts.reauth import RecentAuthRequiredMixin, authenticated_recently, reauth_response
from apps.admin_file.models import RequiredItem
from apps.core.confidentiality import ConfidentialityLevel
from apps.objectives.models import Objective
from apps.vault import services as vault
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import Document, DocumentVersion

from . import services
from .forms import (
    DeadlineForm,
    DecisionForm,
    DocumentForm,
    EditionForm,
    LevelForm,
    PolicyForm,
    PreprintForm,
    RequestForm,
    ResubmitForm,
    StatusForm,
    SubmissionForm,
    VenueForm,
)
from .models import (
    ApprovalDecision,
    Deadline,
    Edition,
    StApproval,
    Submission,
    SubmissionDocument,
    Venue,
)

CONFLICTS = frozenset(
    {
        "stale",
        "frozen",
        "transition",
        "terminal",
        "missing_approval",
        "approval_outdated",
        "already_pending",
        "decided",
        "approval_pending",
        "already_linked",
        "cancelled",
        "duplicate",
        "not_st_work",
        "no_tutor",
        "tutor_cannot_read",
        "target_locked",
    }
)


def _user(request: HttpRequest) -> User:
    user = request.user
    if not isinstance(user, User):
        raise PermissionDenied
    return user


def _status(exc: ValidationError) -> int:
    return 409 if getattr(exc, "code", None) in CONFLICTS else 400


@dataclass(frozen=True)
class Shown:
    """A sealed text as the page shows it: only readable submissions reach a page
    (SR-01), and every text of a submission is at its level."""

    text: str


def shown(value: Any) -> Shown:
    if value is vault.RESTRICTED:  # a bug upstream: refuse rather than hint (SR-01)
        raise PermissionDenied("a text of an unreadable submission reached a page")
    return Shown(text=str(value or ""))


def _objectives(user: User, thesis: Any) -> list[tuple[str, str]]:
    """Objectives to link a submission to: those the user reads only (SR-01)."""
    objectives = list(Objective.objects.readable_by(user, thesis).order_by("due_date", "pk"))
    opened = vault.open_texts(
        user, thesis, [(o, "title") for o in objectives], action=Action.OBJECTIVE_READ
    )
    return [
        (str(o.pk), f"{shown(opened.get((o.pk, 'title'), '')).text} ({o.due_date:%d/%m/%Y})")
        for o in objectives
    ]


def _documents(user: User, thesis: Any) -> list[tuple[str, str]]:
    candidates = Document.objects.filter(thesis=thesis).order_by("title")
    return [(str(d.pk), d.title) for d in candidates if can_access_document(user, d)]


def _versions(user: User, submission: Submission) -> list[DocumentVersion]:
    """Every version of the documents linked to the submission that the user reads."""
    links = SubmissionDocument.objects.filter(submission=submission).select_related("document")
    readable = [link.document for link in links if can_access_document(user, link.document)]
    return list(
        DocumentVersion.objects.filter(document__in=readable)
        .select_related("document")
        .order_by("document__title", "-version")
    )


def _objective_of(thesis: Any, raw: Any) -> Objective | None:
    if not raw:
        return None
    objective = Objective.objects.filter(pk=raw, thesis=thesis).first()
    if objective is None:
        raise ValidationError(_("Unknown objective."), code="objective")
    return objective


class _Mixin(ThesisPermissionRequiredMixin):
    required_action = Action.CONFERENCE_READ
    request: HttpRequest

    def _url(self, name: str, **kw: Any) -> str:
        return reverse(f"conferences:{name}", kwargs={"thesis_id": self.thesis.pk, **kw})

    def _venue(self, venue_id: Any) -> Venue:
        venue = Venue.objects.filter(pk=venue_id, thesis=self.thesis).first()
        if venue is None:
            raise services.deny_unknown(_user(self.request), self.thesis, "venue")
        return venue

    def _edition(self, edition_id: Any) -> Edition:
        edition = (
            Edition.objects.filter(pk=edition_id, thesis=self.thesis)
            .select_related("venue")
            .first()
        )
        if edition is None:
            raise services.deny_unknown(_user(self.request), self.thesis, "edition")
        return edition

    def _deadline(self, deadline_id: Any) -> Deadline:
        deadline = Deadline.objects.filter(pk=deadline_id, thesis=self.thesis).first()
        if deadline is None:
            raise services.deny_unknown(_user(self.request), self.thesis, "deadline")
        return deadline

    def _submission(self, submission_id: Any, operation: str) -> Submission:
        return services.submission_for(
            _user(self.request), self.thesis, submission_id, operation=operation
        )


# --------------------------------------------------------------------------- catalogue


class DeadlinesView(_Mixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = _user(request)
        deadlines = [
            services.deadline_display(d) for d in services.upcoming_deadlines(user, self.thesis)
        ]
        context = {
            "thesis": self.thesis,
            "deadlines": deadlines,
            "venues": Venue.objects.filter(thesis=self.thesis).prefetch_related("editions"),
            "can_write": can(user, Action.CONFERENCE_WRITE, self.thesis),
            "can_submit": can(user, Action.SUBMISSION_WRITE, self.thesis),
            "review_days": services.review_days(self.thesis),
        }
        return render(request, "conferences/deadlines.html", context)


class NewVenueView(_Mixin, View):
    required_action = Action.CONFERENCE_WRITE

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._page(VenueForm())

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        form = VenueForm(request.POST)
        if not form.is_valid():
            return self._page(form, status=400)
        try:
            venue = services.create_venue(
                thesis=self.thesis, user=_user(request), **form.cleaned_data
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
            return self._page(form, status=_status(exc))
        messages.success(request, _("Venue created."))
        return redirect(self._url("venue", venue_id=venue.pk))

    def _page(self, form: VenueForm, status: int = 200) -> HttpResponse:
        context = {"thesis": self.thesis, "form": form, "venue": None}
        return render(self.request, "conferences/venue_form.html", context, status=status)


class VenueView(_Mixin, View):
    def get(self, request: HttpRequest, *args: Any, venue_id: Any, **kwargs: Any) -> HttpResponse:
        return self._page(self._venue(venue_id))

    def post(
        self, request: HttpRequest, *args: Any, venue_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        """Edit the venue (CONFERENCE_WRITE, decided by the service)."""
        venue = self._venue(venue_id)
        form = VenueForm(request.POST)
        if not form.is_valid():
            return self._page(venue, form=form, status=400)
        data = {k: v for k, v in form.cleaned_data.items() if k != "kind"}
        try:
            services.update_venue(venue=venue, user=_user(request), changes=data)
        except ValidationError as exc:
            form.add_error(None, exc.messages)
            return self._page(venue, form=form, status=_status(exc))
        messages.success(request, _("Venue saved."))
        return redirect(self._url("venue", venue_id=venue.pk))

    def _page(
        self, venue: Venue, *, form: Any = None, edition_form: Any = None, status: int = 200
    ) -> HttpResponse:
        writer = can(_user(self.request), Action.CONFERENCE_WRITE, self.thesis)
        context = {
            "thesis": self.thesis,
            "venue": venue,
            "editions": venue.editions.order_by("-year"),
            "form": form
            or (
                VenueForm(
                    initial={
                        "kind": venue.kind,
                        "acronym": venue.acronym,
                        "name": venue.name,
                        "url": venue.url,
                    }
                )
                if writer
                else None
            ),
            "edition_form": edition_form
            or (EditionForm() if writer and venue.has_editions else None),
            "can_write": writer,
        }
        return render(self.request, "conferences/venue.html", context, status=status)


class NewEditionView(_Mixin, View):
    required_action = Action.CONFERENCE_WRITE
    http_method_names: Sequence[str] = ("post",)

    def post(
        self, request: HttpRequest, *args: Any, venue_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        venue = self._venue(venue_id)
        form = EditionForm(request.POST)
        page = VenueView()
        page.request, page.thesis = request, self.thesis
        if not form.is_valid():
            return page._page(venue, edition_form=form, status=400)
        try:
            edition = services.create_edition(venue=venue, user=_user(request), **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc.messages)
            return page._page(venue, edition_form=form, status=_status(exc))
        messages.success(request, _("Edition created."))
        return redirect(self._url("edition", edition_id=edition.pk))


class EditionView(_Mixin, View):
    def get(self, request: HttpRequest, *args: Any, edition_id: Any, **kwargs: Any) -> HttpResponse:
        return self._page(self._edition(edition_id))

    def post(
        self, request: HttpRequest, *args: Any, edition_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        """Add a deadline (CONFERENCE_WRITE, decided by the service)."""
        edition = self._edition(edition_id)
        form = DeadlineForm(request.POST)
        if not form.is_valid():
            return self._page(edition, deadline_form=form, status=400)
        try:
            services.add_deadline(edition=edition, user=_user(request), **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc.messages)
            return self._page(edition, deadline_form=form, status=_status(exc))
        messages.success(request, _("Deadline added."))
        return redirect(self._url("edition", edition_id=edition.pk))

    def _page(
        self, edition: Edition, *, deadline_form: Any = None, status: int = 200
    ) -> HttpResponse:
        writer = can(_user(self.request), Action.CONFERENCE_WRITE, self.thesis)
        deadlines = Deadline.objects.filter(edition=edition).order_by("due_at")
        context = {
            "thesis": self.thesis,
            "edition": edition,
            "deadlines": [services.deadline_display(d) for d in deadlines],
            "deadline_form": deadline_form or (DeadlineForm() if writer else None),
            "can_write": writer,
        }
        return render(self.request, "conferences/edition.html", context, status=status)


class CancelDeadlineView(_Mixin, View):
    required_action = Action.CONFERENCE_WRITE
    http_method_names: Sequence[str] = ("post",)

    def post(
        self, request: HttpRequest, *args: Any, deadline_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        deadline = self._deadline(deadline_id)
        try:
            services.cancel_deadline(deadline=deadline, user=_user(request))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect(self._url("edition", edition_id=deadline.edition_id))


class PolicyView(_Mixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._page()

    def _page(self, form: Any = None, status: int = 200, error: str = "") -> HttpResponse:
        user = _user(self.request)
        tutor = can(user, Action.VALIDATE_PUBLICATION, self.thesis)
        days = services.review_days(self.thesis)
        contract = RequiredItem.objects.filter(
            thesis=self.thesis, code="collaboration_contract"
        ).first()
        context = {
            "thesis": self.thesis,
            "days": days,
            "form": form or (PolicyForm(initial={"st_review_days": days}) if tutor else None),
            "contract": contract,
            "error": error,
        }
        return render(self.request, "conferences/policy.html", context, status=status)


class EditPolicyView(_Mixin, RecentAuthRequiredMixin, View):
    required_action = Action.VALIDATE_PUBLICATION
    http_method_names: Sequence[str] = ("post",)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        form = PolicyForm(request.POST)
        page = PolicyView()
        page.request, page.thesis = request, self.thesis
        if not form.is_valid():
            return page._page(form, status=400)
        try:
            services.set_review_days(
                thesis=self.thesis,
                user=_user(request),
                days=form.cleaned_data["st_review_days"],
                recent_mfa=authenticated_recently(request),
            )
        except ReauthenticationRequiredError:
            return reauth_response(request, mfa=True)
        except ValidationError as exc:
            return page._page(form, status=_status(exc), error=" ".join(exc.messages))
        messages.success(request, _("ST review period saved."))
        return redirect(self._url("policy"))


# --------------------------------------------------------------------------- submissions


class SubmissionsView(_Mixin, View):
    required_action = Action.SUBMISSION_READ

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = _user(request)
        rows = services.submission_list(user, self.thesis)
        context = {
            "thesis": self.thesis,
            "rows": [(r.submission, shown(r.title)) for r in rows],
            "can_submit": can(user, Action.SUBMISSION_WRITE, self.thesis),
        }
        return render(request, "conferences/submissions.html", context)


def _submission_data(form: SubmissionForm) -> dict[str, Any]:
    data = form.cleaned_data
    venue, edition = data["target"]
    return {
        "venue": venue,
        "edition": edition,
        "title": data["title"],
        "abstract": data["abstract"],
        "external_authors": data["external_authors"],
        "notes": data["notes"],
        # M9b: an empty form (a page of before M9b) is a paper.
        "presentation_kind": data.get("presentation_kind") or "PAPER",
    }


class NewSubmissionView(_Mixin, View):
    required_action = Action.SUBMISSION_WRITE

    def _form(self, *args: Any, **kwargs: Any) -> SubmissionForm:
        user = _user(self.request)
        return SubmissionForm(
            *args,
            user=user,
            thesis=self.thesis,
            objectives=_objectives(user, self.thesis),
            **kwargs,
        )

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._page(self._form(initial={"level": ConfidentialityLevel.CONFIDENTIAL.value}))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        user = _user(request)
        form = self._form(request.POST)
        if not form.is_valid():
            return self._page(form, status=400)
        try:
            submission = services.create_submission(
                thesis=self.thesis,
                user=user,
                level=ConfidentialityLevel(form.cleaned_data["level"]),
                objective=_objective_of(self.thesis, form.cleaned_data["objective"]),
                authors=User.objects.filter(pk__in=form.cleaned_data["authors"]),
                **_submission_data(form),
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
            return self._page(form, status=_status(exc))
        messages.success(request, _("Submission created."))
        return redirect(self._url("submission", submission_id=submission.pk))

    def _page(self, form: SubmissionForm, status: int = 200) -> HttpResponse:
        context = {"thesis": self.thesis, "form": form, "submission": None}
        return render(self.request, "conferences/submission_form.html", context, status=status)


class EditSubmissionView(_Mixin, View):
    required_action = Action.SUBMISSION_WRITE

    def _form(self, *args: Any, **kwargs: Any) -> SubmissionForm:
        user = _user(self.request)
        return SubmissionForm(
            *args, user=user, thesis=self.thesis, objectives=_objectives(user, self.thesis),
            with_level=False, **kwargs,
        )  # fmt: skip

    def get(
        self, request: HttpRequest, *args: Any, submission_id: Any, **kwargs: Any
    ) -> HttpResponse:
        submission = self._submission(submission_id, "edit")
        detail = services.submission_detail(_user(request), submission)
        texts = {n: shown(v).text for n, v in detail.texts.items()}
        target = (
            f"edition:{submission.edition_id}"
            if submission.edition_id
            else f"venue:{submission.venue_id}"
        )
        initial = {
            **texts,
            "target": target,
            "presentation_kind": submission.presentation_kind,
            # Point C: a linked objective the user does not read never reaches the
            # page (neither as a choice nor as an initial value); the server keeps it.
            "objective": str(submission.objective_id or "")
            if submission.objective_id
            and Objective.objects.readable_by(_user(request), self.thesis)
            .filter(pk=submission.objective_id)
            .exists()
            else "",
            "authors": [str(u.pk) for u in detail.authors],
        }
        return self._page(submission, self._form(initial=initial))

    def post(
        self, request: HttpRequest, *args: Any, submission_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        user = _user(request)
        submission = self._submission(submission_id, "edit")
        form = self._form(request.POST)
        if not form.is_valid():
            return self._page(submission, form, status=400)
        try:
            changes = {
                **_submission_data(form),
                "objective": _objective_of(self.thesis, form.cleaned_data["objective"]),
            }
            services.update_submission(submission=submission, user=user, changes=changes)
            services.set_authors(
                submission=submission,
                user=user,
                authors=User.objects.filter(pk__in=form.cleaned_data["authors"]),
            )
        except ValidationError as exc:
            form.add_error(None, exc.messages)
            return self._page(submission, form, status=_status(exc))
        messages.success(request, _("Submission saved."))
        return redirect(self._url("submission", submission_id=submission.pk))

    def _page(
        self, submission: Submission, form: SubmissionForm, status: int = 200
    ) -> HttpResponse:
        context = {"thesis": self.thesis, "form": form, "submission": submission}
        return render(self.request, "conferences/submission_form.html", context, status=status)


class SubmissionView(_Mixin, View):
    required_action = Action.SUBMISSION_READ

    def get(
        self, request: HttpRequest, *args: Any, submission_id: Any, **kwargs: Any
    ) -> HttpResponse:
        return render_submission(self, self._submission(submission_id, "detail"))


def render_submission(
    view: _Mixin, submission: Submission, *, status: int = 200, error: str = "", **forms: Any
) -> HttpResponse:
    user = _user(view.request)
    thesis = view.thesis
    detail = services.submission_detail(user, submission)
    s = detail.submission
    writer = can(user, Action.SUBMISSION_WRITE, thesis)
    tutor = can(user, Action.VALIDATE_PUBLICATION, thesis)
    live = not s.is_terminal
    versions = _versions(user, s) if writer else []
    context: dict[str, Any] = {
        "thesis": thesis,
        "submission": s,
        "texts": {n: shown(v) for n, v in detail.texts.items()},
        "authors": detail.authors,
        # The title of a reviewed document only for who may read the document.
        "approvals": [
            (
                a.approval,
                shown(a.comment),
                a.approval.version.document.title
                if can_access_document(user, a.approval.version.document)
                else None,
            )
            for a in detail.approvals
        ],
        "preprints": [(p.preprint, shown(p.url)) for p in detail.preprints],
        "documents": detail.documents,
        "deadlines": [services.deadline_display(d) for d in detail.deadlines],
        "can_write": writer and live,
        "can_decide": tutor,
        "decision_form": forms.get("decision_form") or (DecisionForm() if tutor else None),
        "error": error,
        "pending": ApprovalDecision.PENDING,
    }
    if writer and live:
        context.update(
            document_form=forms.get("document_form")
            or DocumentForm(documents=_documents(user, thesis)),
            request_form=(
                forms.get("request_form") or RequestForm(versions=versions, required=True)
            )
            if s.st_work and versions
            else None,
            status_form=forms.get("status_form") or StatusForm(versions=versions, required=False),
            preprint_form=forms.get("preprint_form")
            or PreprintForm(versions=versions, required=False),
            level_form=forms.get("level_form")
            or LevelForm(user=user, thesis=thesis, initial={"level": s.level}),
        )
    if writer and s.status in ("REJECTED", "WITHDRAWN"):
        context["resubmit_form"] = forms.get("resubmit_form") or ResubmitForm(thesis=thesis)
    return render(view.request, "conferences/submission.html", context, status=status)


class _SubmissionAction(_Mixin):
    """POST on one submission: the service decides; errors re-render the page."""

    required_action = Action.SUBMISSION_WRITE
    http_method_names: Sequence[str] = ("post",)
    operation = ""
    success = gettext_lazy("Saved.")

    def act(self, submission: Submission, user: User) -> Any:
        raise NotImplementedError

    def post(
        self, request: HttpRequest, *args: Any, submission_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        submission = self._submission(submission_id, self.operation)
        self.kwargs_ = kwargs
        self.forms: dict[str, Any] = {}
        try:
            result = self.act(submission, _user(request))
        except ReauthenticationRequiredError:
            return reauth_response(request, mfa=True)
        except ValidationError as exc:
            return render_submission(
                self, submission, status=_status(exc), error=" ".join(exc.messages), **self.forms
            )
        messages.success(request, str(self.success))
        target = (
            result if isinstance(result, Submission) and result.pk != submission.pk else submission
        )
        return redirect(self._url("submission", submission_id=target.pk))

    def _valid(self, form: Any, key: str) -> dict[str, Any]:
        if not form.is_valid():
            self.forms[key] = form
            raise ValidationError([m for errors in form.errors.values() for m in errors])
        data: dict[str, Any] = form.cleaned_data
        return data

    def _approval(self, submission: Submission) -> StApproval:
        approval = StApproval.objects.filter(
            pk=self.kwargs_["approval_id"], submission=submission
        ).first()
        if approval is None:
            raise services.deny_unknown(_user(self.request), self.thesis, "approval")
        return approval


class DocumentView(_SubmissionAction, View):
    operation = "link_document"
    success = gettext_lazy("Document linked.")

    def act(self, submission: Submission, user: User) -> None:
        form = DocumentForm(self.request.POST, documents=_documents(user, self.thesis))
        data = self._valid(form, "document_form")
        document = Document.objects.get(pk=data["document"], thesis=self.thesis)
        services.link_document(submission=submission, user=user, document=document)


class UnlinkDocumentView(_SubmissionAction, View):
    operation = "unlink_document"
    success = gettext_lazy("Document unlinked.")

    def act(self, submission: Submission, user: User) -> None:
        link = SubmissionDocument.objects.filter(
            pk=self.kwargs_["link_id"], submission=submission
        ).first()
        if link is None:
            raise services.deny_unknown(user, self.thesis, "link")
        services.unlink_document(link=link, user=user)


class RequestView(_SubmissionAction, View):
    operation = "request_approval"
    success = gettext_lazy("ST approval requested.")

    def act(self, submission: Submission, user: User) -> None:
        form = RequestForm(self.request.POST, versions=_versions(user, submission), required=True)
        data = self._valid(form, "request_form")
        services.request_approval(
            submission=submission, user=user, version=data["version"], purpose=data["purpose"]
        )


class WithdrawApprovalView(_SubmissionAction, View):
    operation = "withdraw_approval"
    success = gettext_lazy("Request withdrawn.")

    def act(self, submission: Submission, user: User) -> None:
        services.withdraw_approval(approval=self._approval(submission), user=user)


class DecideView(_SubmissionAction, RecentAuthRequiredMixin, View):
    """The industrial tutor decides: role guard (403) before the second factor."""

    required_action = Action.VALIDATE_PUBLICATION
    operation = "decide_approval"
    success = gettext_lazy("Decision recorded.")

    def act(self, submission: Submission, user: User) -> None:
        approval = self._approval(submission)
        form = DecisionForm(self.request.POST)
        data = self._valid(form, "decision_form")
        services.decide_approval(
            approval=approval,
            user=user,
            decision=data["decision"],
            reason=data["reason"],
            comment=data["comment"],
            recent_mfa=authenticated_recently(self.request),
        )


class StatusView(_SubmissionAction, View):
    operation = "status"
    success = gettext_lazy("Status changed.")

    def act(self, submission: Submission, user: User) -> None:
        form = StatusForm(self.request.POST, versions=_versions(user, submission), required=False)
        data = self._valid(form, "status_form")
        services.set_status(
            submission=submission, user=user, status=data["status"], version=data["version"]
        )


class PreprintView(_SubmissionAction, View):
    operation = "preprint"
    success = gettext_lazy("Preprint recorded.")

    def act(self, submission: Submission, user: User) -> None:
        form = PreprintForm(self.request.POST, versions=_versions(user, submission), required=False)
        data = self._valid(form, "preprint_form")
        services.add_preprint(submission=submission, user=user, **data)


class LevelView(_SubmissionAction, RecentAuthRequiredMixin, View):
    operation = "level"
    success = gettext_lazy("Confidentiality changed.")

    def act(self, submission: Submission, user: User) -> None:
        form = LevelForm(self.request.POST, user=user, thesis=self.thesis)
        data = self._valid(form, "level_form")
        services.change_level(
            submission=submission,
            user=user,
            level=ConfidentialityLevel(data["level"]),
            recent_mfa=authenticated_recently(self.request),
        )


class StWorkOffView(_SubmissionAction, RecentAuthRequiredMixin, View):
    """The tutor only, with a second factor: it removes the ST approval step."""

    required_action = Action.VALIDATE_PUBLICATION
    operation = "st_work"
    success = gettext_lazy("This submission no longer needs ST's approval.")

    def act(self, submission: Submission, user: User) -> None:
        services.set_st_work(
            submission=submission,
            user=user,
            value=False,
            recent_mfa=authenticated_recently(self.request),
        )


class StWorkOnView(_SubmissionAction, View):
    required_action = Action.SUBMISSION_READ  # the student or the tutor: the service decides
    operation = "st_work"
    success = gettext_lazy("This submission needs ST's approval again.")

    def act(self, submission: Submission, user: User) -> None:
        services.set_st_work(submission=submission, user=user, value=True, recent_mfa=False)


class ResubmitView(_SubmissionAction, View):
    operation = "resubmit"
    success = gettext_lazy("New submission created: ST's approval must be requested again.")

    def act(self, submission: Submission, user: User) -> Submission:
        form = ResubmitForm(self.request.POST, thesis=self.thesis)
        data = self._valid(form, "resubmit_form")
        venue, edition = data["target"]
        return services.resubmit_elsewhere(
            submission=submission, user=user, venue=venue, edition=edition
        )


# --------------------------------------------------------------------------- fragments


class ObjectiveSubmissionsView(_Mixin, View):
    """« Linked submissions » on an objective's page (htmx fragment)."""

    required_action = Action.SUBMISSION_READ

    def get(
        self, request: HttpRequest, *args: Any, objective_id: Any, **kwargs: Any
    ) -> HttpResponse:
        user = _user(request)
        objective = Objective.objects.readable_by(user, self.thesis).filter(pk=objective_id).first()
        if objective is None:  # unknown or unreadable: the same refusal (SR-01)
            raise services.deny_unknown(user, self.thesis, "objective")
        rows = services.submissions_for_objective(user, objective)
        context = {"thesis": self.thesis, "rows": [(r.submission, shown(r.title)) for r in rows]}
        return render(request, "conferences/_objective_submissions.html", context)
