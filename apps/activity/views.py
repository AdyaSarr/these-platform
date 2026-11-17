"""Views of the activity app (M11a): thin, the rules are in services.py.

Every view is guarded by permissions.py (VIEW_THESIS: every member but a guest).
Opening an element is a GET whose only effect is to mark it read and redirect;
« mark all read » and the preferences are POST with CSRF.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounts.permissions import Action, ThesisPermissionRequiredMixin

from . import services
from .choices import MODULES, Category, Frequency

MODULE_LABELS = {
    "documents": _("Documents"),
    "admin_file": _("Administrative file"),
    "objectives": _("Objectives"),
    "meetings": _("Meetings"),
    "conferences": _("Conferences"),
    "travel": _("Travel"),
    "team": _("Team"),
}


class _Mixin(ThesisPermissionRequiredMixin):
    required_action = Action.VIEW_THESIS


def _page_number(raw: str | None) -> int:
    try:
        return max(1, min(int(raw or 1), 10_000))
    except ValueError:
        return 1


class FeedView(_Mixin, View):
    def get(self, request: HttpRequest, thesis_id: Any) -> HttpResponse:
        module = request.GET.get("module", "")
        page = services.feed(
            request.user, self.thesis, module=module, page=_page_number(request.GET.get("page"))
        )
        base = reverse("activity:feed", kwargs={"thesis_id": self.thesis.pk})
        filters = [("", _("Everything"), base)] + [
            (m, MODULE_LABELS[m], f"{base}?module={m}") for m in MODULES
        ]
        return render(
            request,
            "activity/feed.html",
            {"thesis": self.thesis, "page": page, "filters": filters, "base": base},
        )


class OpenView(_Mixin, View):
    """GET: mark read, then redirect to the element. Nothing else."""

    def get(self, request: HttpRequest, thesis_id: Any, event_id: Any) -> HttpResponseBase:
        return redirect(services.open_event(request.user, self.thesis, event_id))


class ReadAllView(_Mixin, View):
    http_method_names = ["post"]  # noqa: RUF012 (Django declares it as a list)

    def post(self, request: HttpRequest, thesis_id: Any) -> HttpResponseBase:
        services.mark_all_read(request.user, self.thesis)
        return redirect("activity:feed", thesis_id=self.thesis.pk)


class PreferencesForm(forms.Form):
    info = forms.ChoiceField(label=_("Information"), choices=Frequency.choices)


class PreferencesView(_Mixin, View):
    def _render(self, request: HttpRequest, form: PreferencesForm) -> HttpResponse:
        return render(request, "activity/preferences.html", {"thesis": self.thesis, "form": form})

    def get(self, request: HttpRequest, thesis_id: Any) -> HttpResponse:
        current = services.frequency_of(request.user, self.thesis, Category.INFO)
        return self._render(request, PreferencesForm(initial={"info": current}))

    def post(self, request: HttpRequest, thesis_id: Any) -> HttpResponseBase:
        form = PreferencesForm(request.POST)
        if form.is_valid():
            try:
                services.set_preference(
                    request.user, self.thesis, Category.INFO, form.cleaned_data["info"]
                )
            except ValidationError as error:
                form.add_error(None, error)
            else:
                return redirect("activity:preferences", thesis_id=self.thesis.pk)
        return self._render(request, form)
