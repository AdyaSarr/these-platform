from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import DatabaseError, connection
from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from django.views.generic import TemplateView


class HomeView(LoginRequiredMixin, TemplateView):
    template_name = "core/home.html"


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
