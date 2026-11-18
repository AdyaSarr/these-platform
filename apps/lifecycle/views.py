"""Views of the end of the thesis (M13a): thin, the rules are in services.py.

The page: every member who sees the thesis (VIEW_THESIS). The passages: POST with
CSRF, guarded by permissions.py (DECLARE_DEFENSE or REQUEST_CLOSURE, narrowed by
the status), a recent second factor except to withdraw a request.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.permissions import Action, ThesisPermissionRequiredMixin
from apps.accounts.reauth import RecentAuthRequiredMixin

from . import services
from .models import ClosureRequest


class _Guard(ThesisPermissionRequiredMixin):
    required_action = Action.VIEW_THESIS


def _render(
    request: HttpRequest, thesis: Any, *, error: str = "", status: int = 200
) -> HttpResponse:
    context = {**services.page(request.user, thesis), "error": error}
    return render(request, "lifecycle/page.html", context, status=status)


class PageView(_Guard, View):
    def get(self, request: HttpRequest, thesis_id: Any) -> HttpResponse:
        return _render(request, self.thesis)


class _Passage(_Guard, RecentAuthRequiredMixin, View):
    http_method_names = ["post"]  # noqa: RUF012 (Django declares it as a list)

    def act(self, request: HttpRequest, **kwargs: Any) -> Any:
        raise NotImplementedError

    def post(self, request: HttpRequest, thesis_id: Any, **kwargs: Any) -> HttpResponseBase:
        try:
            self.act(request, **kwargs)
        except ValidationError as exc:
            return _render(request, self.thesis, error=" ".join(exc.messages), status=400)
        return redirect("lifecycle:page", thesis_id=self.thesis.pk)


class DefendView(_Passage):
    required_action = Action.DECLARE_DEFENSE

    def act(self, request: HttpRequest, **kwargs: Any) -> Any:
        try:
            day = dt.date.fromisoformat(request.POST.get("defended_on", ""))
        except ValueError as exc:
            raise ValidationError(_("Give the date of the defence."), code="date") from exc
        return services.declare_defense(
            thesis=self.thesis, user=request.user, defended_on=day, recent_mfa=True
        )


class CancelView(_Passage):
    required_action = Action.DECLARE_DEFENSE

    def act(self, request: HttpRequest, **kwargs: Any) -> Any:
        return services.cancel_defense(
            thesis=self.thesis, user=request.user, reason=request.POST.get("reason", ""),
            recent_mfa=True,
        )  # fmt: skip


class RequestView(_Passage):
    required_action = Action.REQUEST_CLOSURE

    def act(self, request: HttpRequest, **kwargs: Any) -> Any:
        return services.request_closure(
            thesis=self.thesis, user=request.user, reason=request.POST.get("reason", ""),
            recent_mfa=True,
        )  # fmt: skip


def _request_of(thesis: Any, request_id: Any) -> ClosureRequest:
    found = ClosureRequest.objects.filter(pk=request_id, thesis=thesis).first()
    if found is None:
        raise Http404
    return found


class ConfirmView(_Passage):
    required_action = Action.REQUEST_CLOSURE

    def act(self, request: HttpRequest, **kwargs: Any) -> Any:
        closure = _request_of(self.thesis, kwargs["request_id"])
        return services.confirm_closure(request=closure, user=request.user, recent_mfa=True)


class WithdrawView(_Guard, View):
    """No second factor: withdrawing opens nothing."""

    required_action = Action.REQUEST_CLOSURE
    http_method_names = ["post"]  # noqa: RUF012 (Django declares it as a list)

    def post(self, request: HttpRequest, thesis_id: Any, request_id: Any) -> HttpResponseBase:
        closure = _request_of(self.thesis, request_id)
        try:
            services.withdraw_closure(request=closure, user=request.user)
        except ValidationError as exc:
            return _render(request, self.thesis, error=" ".join(exc.messages), status=400)
        return redirect("lifecycle:page", thesis_id=self.thesis.pk)
