from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import DatabaseError, connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from django.views.generic import TemplateView


class HomeView(LoginRequiredMixin, TemplateView):
    """The user's theses (active memberships only); straight to it if there is one."""

    template_name = "core/home.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        from apps.accounts.models import Thesis

        self.theses = list(Thesis.objects.visible_to(request.user).order_by("title"))
        if len(self.theses) == 1:
            return redirect("accounts:dashboard", thesis_id=self.theses[0].pk)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["theses"] = self.theses
        return context


@never_cache
@require_GET
def health(request: HttpRequest) -> JsonResponse:
    """Public liveness/readiness probe. Never reveals why it fails."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """Our page for a refused form (CSRF_FAILURE_VIEW): the sentence of
    docs/ui-notes.md, status 403. ``reason`` is never shown (Django logs it
    already), and nothing of the request is copied into the page: the theme form
    comes back to "/" (apps/ui/context_processors.py)."""
    request.ui_echo_nothing = True  # type: ignore[attr-defined]
    return render(request, "core/csrf_failure.html", status=403)
