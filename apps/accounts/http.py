"""HTTP helpers of the accounts app."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.views.decorators.cache import never_cache


def no_store(view: Callable[..., Any]) -> Callable[..., Any]:
    """never_cache (Cache-Control: no-store...) for a page that shows a secret.

    Marked, because never_cache keeps the wrapped view's name: the test of secret
    pages (test_mfa_secret_pages.py) checks the mark on every such route, so the
    guarantee does not rest on allauth, which protects some of these views itself.
    """
    wrapped = never_cache(view)
    wrapped.secret_page_no_store = True  # type: ignore[attr-defined]
    return wrapped
