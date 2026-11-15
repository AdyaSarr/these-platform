from allauth.account import views as allauth_views
from allauth.mfa.recovery_codes import views as recovery_views
from allauth.mfa.totp import views as totp_views
from django.conf import settings
from django.contrib.auth import views as auth_views
from django.http import Http404, HttpRequest, HttpResponse
from django.urls import URLPattern, URLResolver, include, path, re_path

from apps.accounts import invitation_views
from apps.accounts.http import no_store
from apps.accounts.reauth import recent_auth_required


def closed(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
    """allauth routes we do not offer: answer as if they did not exist."""
    raise Http404


# allauth features turned off (sign-up, e-mail management and verification,
# login and re-authentication by e-mailed code). Declared BEFORE allauth's own
# URLs: the first matching route wins. tests/test_auth_routes.py checks them.
_CLOSED_ACCOUNT_ROUTES = [
    path("accounts/signup/", closed),
    path("accounts/email/", closed),
    path("accounts/confirm-email/", closed),
    re_path(r"^accounts/confirm-email/(?P<key>[-:\w]+)/$", closed),
    path("accounts/login/code/confirm/", closed),
    path("accounts/reauthenticate/code/", closed),
    path("accounts/reauthenticate/code/confirm/", closed),
]

# Recovery codes: allauth only requires a signed-in user to show or download
# them. We require a second factor less than 10 minutes old (decisions.md). The
# pages show (or download) the codes: never cached (no-store), redirects included.
_RECOVERY_CODE_ROUTES = [
    path(
        "accounts/2fa/recovery-codes/",
        no_store(recent_auth_required(recovery_views.view_recovery_codes)),
        name="mfa_view_recovery_codes",
    ),
    path(
        "accounts/2fa/recovery-codes/generate/",
        no_store(recent_auth_required(recovery_views.generate_recovery_codes)),
        name="mfa_generate_recovery_codes",
    ),
    path(
        "accounts/2fa/recovery-codes/download/",
        no_store(recent_auth_required(recovery_views.download_recovery_codes)),
        name="mfa_download_recovery_codes",
    ),
]

# TOTP activation: the page shows the secret (QR code and field): never cached
# (no-store), declared before allauth's own route, same name.
_TOTP_ROUTES = [
    path(
        "accounts/2fa/totp/activate/",
        no_store(totp_views.activate_totp),
        name="mfa_activate_totp",
    ),
]

urlpatterns: list[URLPattern | URLResolver] = [
    path("", include("apps.core.urls")),
    # Every page of a thesis lives under /t/<id>/ and is guarded by permissions.py.
    path("t/<uuid:thesis_id>/", include("apps.accounts.urls")),
    path("t/<uuid:thesis_id>/documents/", include("apps.vault.urls")),
    path("t/<uuid:thesis_id>/admin-file/", include("apps.admin_file.urls")),
    path("t/<uuid:thesis_id>/objectives/", include("apps.objectives.urls")),
    path("t/<uuid:thesis_id>/meetings/", include("apps.meetings.urls")),
    path("t/<uuid:thesis_id>/conferences/", include("apps.conferences.urls")),
    path("t/<uuid:thesis_id>/travel/", include("apps.travel.urls")),
    # Invitation acceptance (apps/accounts/invitation_views.py). "accept/" and
    # "logout/" first: the <str:token> route would match them otherwise.
    path("invitation/", invitation_views.invitation_continue, name="invitation_continue"),
    path(
        "invitation/accept/",
        invitation_views.AcceptInvitationView.as_view(),
        name="invitation_accept",
    ),
    path("invitation/logout/", invitation_views.invitation_logout, name="invitation_logout"),
    path("invitation/<str:token>/", invitation_views.invitation_link, name="invitation_link"),
    # Sign-in through allauth (password, then second factor). The "login" and
    # "logout" names are kept for the templates; logout stays POST-only.
    path("accounts/login/", allauth_views.login, name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    *_CLOSED_ACCOUNT_ROUTES,
    *_RECOVERY_CODE_ROUTES,
    *_TOTP_ROUTES,
    path("accounts/", include("allauth.urls")),  # also mounts allauth.mfa under 2fa/
]

# Admin: dev and test only (the route does not exist in prod). Its login page
# goes through allauth, so it requires the second factor too.
if settings.ADMIN_ENABLED:
    from allauth.account.decorators import secure_admin_login
    from django.contrib import admin

    admin.site.login = secure_admin_login(admin.site.login)  # type: ignore[method-assign]
    urlpatterns.append(path("admin/", admin.site.urls))
