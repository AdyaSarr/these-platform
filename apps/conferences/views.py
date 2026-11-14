"""Pages of the conferences (mounted under /t/<thesis_id>/conferences/)."""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views import View

from apps.accounts.permissions import Action, ThesisPermissionRequiredMixin


class _Stub(ThesisPermissionRequiredMixin, View):
    required_action = Action.CONFERENCE_READ

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return HttpResponse(status=204)


class DeadlinesView(_Stub):
    pass


class PolicyView(_Stub):
    pass


class EditionView(_Stub):
    pass


class SubmissionView(_Stub):
    required_action = Action.SUBMISSION_READ
