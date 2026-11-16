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
from dataclasses import dataclass, field
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views import View

from apps.accounts.models import Membership, User
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
from .forms import (
    CustomItemForm,
    DecisionForm,
    DueDateForm,
    ProposalForm,
    ReopenForm,
    SubmitForm,
)
from .models import (
    DueDateProposal,
    ItemApproval,
    ItemReopening,
    ItemStatus,
    RequiredItem,
    Section,
)
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
    # Approvals of the current cycle while they count (SUBMITTED, VALIDATED) ...
    approvals: QuerySet[ItemApproval]
    # ... or once cancelled (rejected or withdrawn cycle), never shown as valid.
    cancelled_approvals: QuerySet[ItemApproval]
    reopening: ItemReopening | None  # the cycle reopened, while the item is TODO
    pending: DueDateProposal | None
    document: Document | None  # None also when the user cannot read it
    has_hidden_document: bool
    stale: bool
    overdue: bool  # still to deposit (TODO, REJECTED) and past its due date
    validators: list[str]
    # Deposited: days since the deposit, in Paris dates (None: deposited before
    # RequiredItem.submitted_at existed, or not deposited).
    waiting_days: int | None = None
    # Validator roles nobody holds in the team (never for a validated item), and
    # whether the viewer may invite them.
    missing_validators: list[str] = field(default_factory=list)
    can_invite: bool = False
    submit_form: SubmitForm | None = None
    decision_form: DecisionForm | None = None
    reopen_form: ReopenForm | None = None
    due_form: DueDateForm | None = None
    due_url: str = ""
    propose_form: ProposalForm | None = None
    error: str = ""
    errors: list[str] | None = None


def build_row(user: User, item: RequiredItem, **forms: Any) -> Row:
    custom = item.code.startswith("custom-")
    pending = None if custom else services.pending_proposal(item)
    rights = services.item_rights(user, item, pending=pending)
    entry = catalog.by_code(item.code)
    readable = item.document is not None and can_access_document(user, item.document)
    status = ItemStatus(item.status)
    cycle = ItemApproval.objects.filter(item=item, cycle=item.cycle).select_related("user")
    if not item.cycle:
        cycle = ItemApproval.objects.none()
    reopening = None
    if status == ItemStatus.TODO and item.cycle:
        reopening = (
            ItemReopening.objects.filter(item=item, cycle=item.cycle)
            .select_related("reopened_by")
            .first()
        )
    counting = status in (ItemStatus.SUBMITTED, ItemStatus.VALIDATED)
    row = Row(
        item=item,
        label=services.label_of(item),
        warning=str(entry.deposit_warning) if entry is not None else "",
        rights=rights,
        approvals=cycle if counting else ItemApproval.objects.none(),
        cancelled_approvals=(
            ItemApproval.objects.none() if counting or reopening is not None else cycle
        ),
        reopening=reopening,
        pending=pending,
        document=item.document if readable else None,
        has_hidden_document=item.document is not None and not readable,
        stale=services.is_stale(item),
        overdue=item.due_date is not None
        and status in (ItemStatus.TODO, ItemStatus.REJECTED)
        and item.due_date < timezone.localdate(),
        validators=[str(Role(r).label) for r in item.validator_roles],
        error=forms.pop("error", ""),
        errors=forms.pop("errors", None),
    )
    if status == ItemStatus.SUBMITTED and item.submitted_at is not None:
        deposited = timezone.localdate(item.submitted_at)
        row.waiting_days = (timezone.localdate() - deposited).days
    if status != ItemStatus.VALIDATED:
        held = set(
            Membership.objects.active()
            .filter(thesis_id=item.thesis_id)
            .values_list("role", flat=True)
        )
        row.missing_validators = [str(Role(r).label) for r in item.validator_roles if r not in held]
        row.can_invite = bool(row.missing_validators) and can(
            user, Action.INVITE_MEMBER, item.thesis
        )
    if rights.submit:
        row.submit_form = forms.get("submit_form") or SubmitForm(item=item, user=user)
    if rights.decide:
        row.decision_form = forms.get("decision_form") or DecisionForm()
    if rights.reopen:
        reasons = services.reopen_reasons_for(user, item)
        row.reopen_form = forms.get("reopen_form") or ReopenForm(reasons=reasons)
    if rights.edit_due:
        row.due_form = forms.get("due_form") or DueDateForm(initial={"due_date": item.due_date})
        url_name = "admin_file:custom_due_date" if custom else "admin_file:due_date"
        row.due_url = reverse(url_name, kwargs={"thesis_id": item.thesis_id, "item_id": item.pk})
    if rights.propose_due:
        row.propose_form = forms.get("propose_form") or ProposalForm()
    return row


class _FileMixin(ThesisPermissionRequiredMixin):
    required_action = Action.CIFRE_FILE_READ  # the lowest read right of the file
    request: HttpRequest

    def _fresh(self, item_id: Any, operation: str) -> RequiredItem:
        item = services.item_for(_user(self.request), self.thesis, item_id, operation=operation)
        return RequiredItem.objects.select_related("thesis", "document").get(pk=item.pk)

    def _file_url(self) -> str:
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


class ValidatorGateMixin:
    """After the thesis guard, BEFORE the re-authentication: a role that is not a
    validator of the item gets an audited 403 without being asked for a second
    factor (decision 35). The service decides again."""

    request: HttpRequest
    thesis: Any
    operation: str

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        user = _user(request)
        item = services.item_for(user, self.thesis, kwargs.get("item_id"), operation=self.operation)
        services.require_due_date_validator(user, item, operation=self.operation)
        response: HttpResponseBase = super().dispatch(request, *args, **kwargs)  # type: ignore[misc]
        return response


class DueDateView(_ItemAction, ValidatorGateMixin, RecentAuthRequiredMixin, View):
    """A validator changes the due date of a catalogue item (recent second factor)."""

    operation = "update"
    success = gettext_lazy("Due date saved.")

    def act(self, item: RequiredItem, user: User) -> None:
        data = self._valid(DueDateForm(self.request.POST), "due_form")
        services.update_due_date(item=item, user=user, due_date=data.get("due_date"))


class CustomDueDateView(_ItemAction, View):
    """The student changes the due date of a custom item (the rule of M3)."""

    required_action = Action.ADMIN_FILE_WRITE
    operation = "update"
    success = gettext_lazy("Due date saved.")

    def act(self, item: RequiredItem, user: User) -> None:
        data = self._valid(DueDateForm(self.request.POST), "due_form")
        services.update_due_date(item=item, user=user, due_date=data.get("due_date"))


class ProposeDueDateView(_ItemAction, View):
    required_action = Action.ADMIN_FILE_WRITE
    operation = "propose_due_date"
    success = gettext_lazy("Due date proposed: the validators are notified.")

    def act(self, item: RequiredItem, user: User) -> None:
        data = self._valid(ProposalForm(self.request.POST), "propose_form")
        services.propose_due_date(
            item=item,
            user=user,
            due_date=data["due_date"],
            reason=data["reason"],
            comment=data["comment"],
        )


class _ProposalAction(_ItemAction):
    kwargs: dict[str, Any]  # set by View.setup()

    def proposal(self, item: RequiredItem, user: User) -> DueDateProposal:
        return services.proposal_for(
            user, item, self.kwargs.get("proposal_id"), operation=self.operation
        )


class AcceptDueDateView(_ProposalAction, ValidatorGateMixin, RecentAuthRequiredMixin, View):
    operation = "accept_due_date"
    success = gettext_lazy("Proposed due date accepted.")

    def act(self, item: RequiredItem, user: User) -> None:
        services.accept_due_date(proposal=self.proposal(item, user), user=user)


class RefuseDueDateView(_ProposalAction, ValidatorGateMixin, RecentAuthRequiredMixin, View):
    operation = "refuse_due_date"
    success = gettext_lazy("Proposed due date refused.")

    def act(self, item: RequiredItem, user: User) -> None:
        services.refuse_due_date(proposal=self.proposal(item, user), user=user)


class WithdrawDueDateView(_ProposalAction, View):
    required_action = Action.ADMIN_FILE_WRITE
    operation = "withdraw_due_date"
    success = gettext_lazy("Proposal withdrawn.")

    def act(self, item: RequiredItem, user: User) -> None:
        services.withdraw_due_date_proposal(proposal=self.proposal(item, user), user=user)


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
        reasons = services.reopen_reasons_for(user, item)
        data = self._valid(ReopenForm(self.request.POST, reasons=reasons), "reopen_form")
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
