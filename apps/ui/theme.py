"""Light / dark preference: a cookie read by the server (no script)."""

from __future__ import annotations

from django.http import HttpRequest

COOKIE_NAME = "theme"
THEMES = ("auto", "light", "dark")
MAX_AGE = 365 * 24 * 3600


def theme_of(request: HttpRequest) -> str:
    """The viewer's choice, from the closed list only; "auto" otherwise."""
    value = request.COOKIES.get(COOKIE_NAME, "")
    return value if value in THEMES else "auto"


def back_to(request: HttpRequest) -> str:
    """Where the theme form comes back to: the path of a thesis page (ids only),
    "/" anywhere else. Never another path: an invitation or a password-reset link
    holds its token in the path, which must not be copied into a form."""
    path = request.path
    return path if path.startswith("/t/") and "?" not in path else "/"
