"""Pages of the administrative file (mounted under /t/<thesis_id>/admin-file/).

Thin views. Order of checks, by class order (the M2 lesson):
1. ThesisPermissionRequiredMixin (403, THESIS_ACCESS_DENIED audited);
2. RecentAuthRequiredMixin for every decision (approve, reject, reopen):
   listed AFTER the guard and BEFORE View, whose dispatch() does not call super();
3. the service, which decides per item and audits its own refusals.

An htmx request gets the item's fragment back (also for 400 and 409, which the
htmx-config of base.html swaps); a plain request is redirected (success) or
gets the item page with the message (error).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
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
from apps.accounts.reauth import RecentAuthRequiredMixin
from apps.accounts.roles import Role
from apps.vault.models import Document

from . import catalog, services
from .forms import CustomItemForm, DecisionForm, DueDateForm, SubmitForm
from .models import ItemApproval, ItemStatus, RequiredItem, Section
from .transitions import FrozenFileError, InvalidTransitionError, StaleSubmissionError

STALE_MESSAGE = gettext_lazy(
    "The document has changed since it was submitted: withdraw it and submit it again, "
    "or reject it."
)
RELOAD_MESSAGE = gettext_lazy("The item has changed, reload the page.")
FROZEN_MESSAGE = gettext_lazy("The thesis is finished: its administrative file is frozen.")


def _user(request: HttpRequest) -> User:
    user = request.user
    if not isinstance(user, User):  # the thesis guard already refused anonymous users
        raise PermissionDenied
    return user


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


@dataclass
class Row:
    """Everything one item's fragment shows. ``rights`` only shows or hides controls."""

    item: RequiredItem
    label: str
    warning: str
    rights: services.ItemRights
    approvals: QuerySet[ItemApproval]
    document: Document | None  # None also when the user cannot read it
    has_hidden_document: bool
    stale: bool
    overdue: bool
    validators: list[str]
    submit_form: SubmitForm | None = None
    decision_form: DecisionForm | None = None
    due_form: DueDateForm | None = None
    error: str = ""
    errors: list[str] | None = None


def build_row(user: User, item: RequiredItem, **forms: Any) -> Row:
    rights = services.item_rights(user, item)
    entry = catalog.by_code(item.code)
    readable = item.document is not None and can_access_document(user, item.document)
    approvals = ItemApproval.objects.filter(item=item, cycle=item.cycle).select_related("user")
    row = Row(
        item=item,
        label=services.label_of(item),
        warning=str(entry.deposit_warning) if entry is not None else "",
        rights=rights,
        approvals=approvals if item.cycle else ItemApproval.objects.none(),
        document=item.document if readable else None,
        has_hidden_document=item.document is not None and not readable,
        stale=services.is_stale(item),
        overdue=item.due_date is not None
        and item.status != ItemStatus.VALIDATED
        and item.due_date < timezone.localdate(),
        validators=[str(Role(r).label) for r in item.validator_roles],
        error=forms.pop("error", ""),
        errors=forms.pop("errors", None),
    )
    if rights.submit:
        row.submit_form = forms.get("submit_form") or SubmitForm(item=item, user=user)
    if rights.decide or rights.reopen:
        row.decision_form = forms.get("decision_form") or DecisionForm()
    if rights.edit_due:
        row.due_form = forms.get("due_form") or DueDateForm(initial={"due_date": item.due_date})
    return row


class _FileMixin(ThesisPermissionRequiredMixin):
    required_action = Action.CIFRE_FILE_READ  # the lowest read right of the file
    request: HttpRequest

    def _fresh(self, item_id: Any, operation: str) -> RequiredItem:
        item = services.item_for(_user(self.request), self.thesis, item_id, operation=operation)
        return RequiredItem.objects.select_related("thesis", "document").get(pk=item.pk)

    def _file_url(self) -> str:
        from django.urls import reverse

        return reverse("admin_file:file", kwargs={"thesis_id": self.thesis.pk})

    def _respond(self, item: RequiredItem, *, status: int = 200, **forms: Any) -> HttpResponse:
        row = build_row(_user(self.request), item, **forms)
        context = {"thesis": self.thesis, "row": row}
        if _is_htmx(self.request):
            return render(self.request, "admin_file/item_row.html", context, status=status)
        return render(self.request, "admin_file/item_page.html", context, status=status)


class FileView(_FileMixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = _user(request)
        items = (
            services.visible_items(user, self.thesis)
            .select_related("thesis", "document")
            .order_by("section", "due_date", "code")
        )
        sections = [
            (Section(s).label, [build_row(user, i) for i in items if i.section == s])
            for s in (Section.CIFRE, Section.ACADEMIC)
        ]
        progress = services.progress(user, self.thesis)
        return render(
            request,
            "admin_file/file.html",
            {
                "thesis": self.thesis,
                "sections": [(label, rows) for label, rows in sections if rows],
                "progress": progress,
                "incomplete": self.thesis.status == self.thesis.Status.ACTIVE
                and not progress.complete,
                "can_create": can(user, Action.ADMIN_FILE_WRITE, self.thesis),
            },
        )


class ItemView(_FileMixin, View):
    def get(self, request: HttpRequest, *args: Any, item_id: Any, **kwargs: Any) -> HttpResponse:
        return self._respond(self._fresh(item_id, "detail"))


class _ItemAction(_FileMixin):
    """POST on one item: the service decides; errors become 400 / 409 messages."""

    http_method_names: Sequence[str] = ("post",)  # same type as View's (django-stubs)
    operation = ""
    success = gettext_lazy("Saved.")

    def act(self, item: RequiredItem, user: User) -> None:
        raise NotImplementedError

    def post(
        self, request: HttpRequest, *args: Any, item_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        user = _user(request)
        item = self._fresh(item_id, self.operation)
        self.forms: dict[str, Any] = {}
        try:
            self.act(item, user)
        except StaleSubmissionError:
            return self._error(item_id, 409, str(STALE_MESSAGE))
        except FrozenFileError:
            return self._error(item_id, 409, str(FROZEN_MESSAGE))
        except InvalidTransitionError:
            return self._error(item_id, 409, str(RELOAD_MESSAGE))
        except ValidationError as exc:
            return self._error(item_id, 400, " ".join(exc.messages))
        fresh = RequiredItem.objects.select_related("thesis", "document").get(pk=item.pk)
        if _is_htmx(request):
            return self._respond(fresh)
        messages.success(request, str(self.success))
        return redirect(self._file_url())

    def _error(self, item_id: Any, status: int, message: str) -> HttpResponse:
        item = RequiredItem.objects.select_related("thesis", "document").get(pk=item_id)
        return self._respond(item, status=status, error=message, **self.forms)

    def _valid(self, form: Any, key: str) -> Any:
        """The cleaned data of ``form``, or a ValidationError (400) keeping the form."""
        if not form.is_valid():
            self.forms[key] = form
            raise ValidationError([m for errors in form.errors.values() for m in errors])
        return form.cleaned_data


class SubmitView(_ItemAction, View):
    required_action = Action.ADMIN_FILE_WRITE
    operation = "submit"
    success = gettext_lazy("Item submitted.")

    def act(self, item: RequiredItem, user: User) -> None:
        form = SubmitForm(self.request.POST, self.request.FILES, item=item, user=user)
        data = self._valid(form, "submit_form")
        level = data.get("level") or None
        services.submit(
            item=item,
            user=user,
            upload=data.get("file") or None,
            document=data.get("document_obj"),
            level=None if item.document is not None or not level else level,
        )


class WithdrawView(_ItemAction, View):
    required_action = Action.ADMIN_FILE_WRITE
    operation = "withdraw"
    success = gettext_lazy("Submission withdrawn.")

    def act(self, item: RequiredItem, user: User) -> None:
        services.withdraw(item=item, user=user)


class DueDateView(_ItemAction, View):
    required_action = Action.ADMIN_FILE_WRITE
    operation = "update"
    success = gettext_lazy("Due date saved.")

    def act(self, item: RequiredItem, user: User) -> None:
        data = self._valid(DueDateForm(self.request.POST), "due_form")
        services.update_due_date(item=item, user=user, due_date=data.get("due_date"))


class ApproveView(_ItemAction, RecentAuthRequiredMixin, View):
    operation = "approve"
    success = gettext_lazy("Approval recorded.")

    def act(self, item: RequiredItem, user: User) -> None:
        services.approve(item=item, user=user)


class RejectView(_ItemAction, RecentAuthRequiredMixin, View):
    operation = "reject"
    success = gettext_lazy("Item rejected.")

    def act(self, item: RequiredItem, user: User) -> None:
        data = self._valid(DecisionForm(self.request.POST), "decision_form")
        services.reject(item=item, user=user, reason=data["reason"], comment=data["comment"])


class ReopenView(_ItemAction, RecentAuthRequiredMixin, View):
    operation = "reopen"
    success = gettext_lazy("Item reopened.")

    def act(self, item: RequiredItem, user: User) -> None:
        data = self._valid(DecisionForm(self.request.POST), "decision_form")
        services.reopen(item=item, user=user, reason=data["reason"], comment=data["comment"])


class NewItemView(_FileMixin, View):
    required_action = Action.ADMIN_FILE_WRITE

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._page(CustomItemForm())

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        form = CustomItemForm(request.POST)
        if not form.is_valid():
            return self._page(form, status=400)
        data = form.cleaned_data
        try:
            services.create_custom_item(
                thesis=self.thesis,
                user=_user(request),
                label=data["label"],
                section=Section(data["section"]),
                validator_roles=data["validators"],
                default_level=data["default_level"],
                mandatory=data["mandatory"],
                attachment_required=data["attachment_required"],
                due_date=data.get("due_date"),
            )
        except FrozenFileError:
            form.add_error(None, str(FROZEN_MESSAGE))
            return self._page(form, status=409)
        except ValidationError as exc:
            form.add_error(None, exc)
            return self._page(form, status=400)
        messages.success(request, _("Item added."))
        return redirect(self._file_url())

    def _page(self, form: CustomItemForm, status: int = 200) -> HttpResponse:
        return render(
            request=self.request,
            template_name="admin_file/new_item.html",
            context={"thesis": self.thesis, "form": form},
            status=status,
        )
