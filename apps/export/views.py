"""Views of the export (M13b): thin, the rules are in services.py.

The guard (ThesisPermissionRequiredMixin) comes before the second factor
(RecentAuthRequiredMixin): whoever lacks the right gets the refusal of right, and
never learns of the hourly limit or of a second factor to give.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.core.exceptions import ValidationError
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    StreamingHttpResponse,
)
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.permissions import Action, ThesisPermissionRequiredMixin
from apps.accounts.reauth import RecentAuthRequiredMixin

from . import services
from .models import StExportConsent


def _page(request: HttpRequest, thesis: Any, *, error: str = "", status: int = 200) -> HttpResponse:
    context = {**services.page(request.user, thesis), "error": error}
    return render(request, "export/page.html", context, status=status)


def _st_page(
    request: HttpRequest, thesis: Any, *, error: str = "", status: int = 200
) -> HttpResponse:
    context = {**services.st_page(request.user, thesis), "error": error}
    return render(request, "export/st.html", context, status=status)


class PageView(ThesisPermissionRequiredMixin, View):
    required_action = Action.THESIS_EXPORT

    def get(self, request: HttpRequest, thesis_id: Any) -> HttpResponse:
        return _page(request, self.thesis)


class DownloadView(ThesisPermissionRequiredMixin, RecentAuthRequiredMixin, View):
    required_action = Action.THESIS_EXPORT
    http_method_names = ["post"]  # noqa: RUF012 (Django declares it as a list)

    def post(self, request: HttpRequest, thesis_id: Any) -> HttpResponseBase:
        try:
            started = services.start_export(
                thesis=self.thesis, user=request.user,
                all_versions=request.POST.get("all_versions") == "on", recent_mfa=True,
            )  # fmt: skip
        except services.ExportRateLimitedError as exc:
            return _page(request, self.thesis, error=" ".join(exc.messages), status=429)
        except services.ExportTooLargeError as exc:
            return _page(request, self.thesis, error=" ".join(exc.messages), status=413)
        response = StreamingHttpResponse(started.chunks(), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="{started.filename}"'
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "no-store"
        return response


class StPageView(ThesisPermissionRequiredMixin, View):
    required_action = Action.SET_ST_CONFIDENTIALITY

    def get(self, request: HttpRequest, thesis_id: Any) -> HttpResponse:
        return _st_page(request, self.thesis)


class StDateView(ThesisPermissionRequiredMixin, RecentAuthRequiredMixin, View):
    required_action = Action.SET_ST_CONFIDENTIALITY
    http_method_names = ["post"]  # noqa: RUF012 (Django declares it as a list)

    def post(self, request: HttpRequest, thesis_id: Any) -> HttpResponseBase:
        raw = request.POST.get("until", "").strip()
        try:
            until = dt.date.fromisoformat(raw) if raw else None
        except ValueError:
            return _st_page(request, self.thesis, error=_("Give a date."), status=400)
        try:
            services.set_st_confidential_until(
                thesis=self.thesis, user=request.user, until=until, recent_mfa=True
            )
        except ValidationError as exc:
            return _st_page(request, self.thesis, error=" ".join(exc.messages), status=400)
        return redirect("export:st", thesis_id=self.thesis.pk)


class StGrantView(ThesisPermissionRequiredMixin, RecentAuthRequiredMixin, View):
    required_action = Action.GRANT_ST_EXPORT
    http_method_names = ["post"]  # noqa: RUF012 (Django declares it as a list)

    def post(self, request: HttpRequest, thesis_id: Any) -> HttpResponseBase:
        services.grant_st_consent(thesis=self.thesis, user=request.user, recent_mfa=True)
        return redirect("export:st", thesis_id=self.thesis.pk)


class StRevokeView(ThesisPermissionRequiredMixin, View):
    """No second factor: revoking closes an access."""

    required_action = Action.GRANT_ST_EXPORT
    http_method_names = ["post"]  # noqa: RUF012 (Django declares it as a list)

    def post(self, request: HttpRequest, thesis_id: Any, consent_id: Any) -> HttpResponseBase:
        consent = StExportConsent.objects.filter(pk=consent_id, thesis=self.thesis).first()
        if consent is None:
            raise Http404
        services.revoke_st_consent(consent=consent, user=request.user)
        return redirect("export:st", thesis_id=self.thesis.pk)
