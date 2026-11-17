"""Views of the activity app (M11a): thin, the rules are in services.py.

Every view is guarded by permissions.py (VIEW_THESIS: every member but a guest).
Opening an element is a GET whose only effect is to mark it read and redirect;
« mark all read » and the preferences are POST with CSRF.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounts.permissions import Action, ThesisPermissionRequiredMixin

from . import services
from .choices import MODULES, Category, Frequency, allowed_frequencies
from .models import NotificationPreference

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


class DigestView(_Mixin, View):
    """The « Résumé » a digest e-mail links to: its recipient only."""

    def get(self, request: HttpRequest, thesis_id: Any, digest_id: Any) -> HttpResponse:
        page = services.digest_page(request.user, self.thesis, digest_id)
        return render(request, "activity/digest.html", {"thesis": self.thesis, "page": page})


def _choices(category: str) -> list[tuple[str, Any]]:
    allowed = allowed_frequencies(category)
    return [(value, label) for value, label in Frequency.choices if value in allowed]


class PreferencesForm(forms.Form):
    """M11b: the three categories; the service refuses what is not allowed too."""

    info = forms.ChoiceField(label=_("Information"), choices=_choices(Category.INFO))
    action = forms.ChoiceField(
        label=_("Asks you to act"), choices=_choices(Category.ACTION), required=False
    )
    deadline = forms.ChoiceField(
        label=_("Deadline reminders"), choices=_choices(Category.DEADLINE), required=False
    )


FIELDS = (("info", Category.INFO), ("action", Category.ACTION), ("deadline", Category.DEADLINE))


class PreferencesView(_Mixin, View):
    def _render(self, request: HttpRequest, form: PreferencesForm) -> HttpResponse:
        return render(request, "activity/preferences.html", {"thesis": self.thesis, "form": form})

    def get(self, request: HttpRequest, thesis_id: Any) -> HttpResponse:
        initial = {
            name: services.frequency_of(request.user, self.thesis, category)
            for name, category in FIELDS
        }
        return self._render(request, PreferencesForm(initial=initial))

    def post(self, request: HttpRequest, thesis_id: Any) -> HttpResponseBase:
        form = PreferencesForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    for name, category in FIELDS:
                        wanted = form.cleaned_data[name]  # absent: unchanged
                        stored = NotificationPreference.objects.filter(
                            user_id=str(request.user.pk), thesis=self.thesis, category=category
                        ).exists()
                        current = services.frequency_of(request.user, self.thesis, category)
                        # A choice made on the page is kept, even equal to the default:
                        # a later change of the defaults does not change it.
                        if wanted and (wanted != current or not stored):
                            services.set_preference(request.user, self.thesis, category, wanted)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                return redirect("activity:preferences", thesis_id=self.thesis.pk)
        return self._render(request, form)
