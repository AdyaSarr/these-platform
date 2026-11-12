from django.conf import settings
from django.contrib.auth import views as auth_views
from django.urls import URLPattern, URLResolver, include, path

urlpatterns: list[URLPattern | URLResolver] = [
    path("", include("apps.core.urls")),
    # Every page of a thesis lives under /t/<id>/ and is guarded by permissions.py.
    path("t/<uuid:thesis_id>/", include("apps.accounts.urls")),
    # Plain password login until M1 adds mandatory MFA.
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(redirect_authenticated_user=True),
        name="login",
    ),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
]

# Admin: dev and test only. In prod the route does not exist at all until M1.
if settings.ADMIN_ENABLED:
    from django.contrib import admin

    urlpatterns.append(path("admin/", admin.site.urls))
