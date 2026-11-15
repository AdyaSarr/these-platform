"""Cross-cutting checks on every thesis route (``/t/<thesis_id>/...``), present and future.

1. Every such route is guarded by permissions.py (CLAUDE.md rule 4).
2. Acceptance: a removed member gets 403 on every thesis page, with the session
   they already had (no new login).
"""

import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone

from apps.accounts.models import Membership
from apps.accounts.permissions import THESIS_URL_KWARG, ThesisPermissionRequiredMixin
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory


def _walk(patterns: list[Any], prefix: str = "") -> Iterator[tuple[str, URLPattern]]:
    for p in patterns:
        route = prefix + str(p.pattern)
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, route)
        elif isinstance(p, URLPattern):
            yield route, p


def thesis_routes() -> list[tuple[str, URLPattern]]:
    return [
        (r, p) for r, p in _walk(get_resolver().url_patterns) if f"<uuid:{THESIS_URL_KWARG}>" in r
    ]


def _is_guarded(p: URLPattern) -> bool:
    cb = p.callback
    view_class = getattr(cb, "view_class", None)
    if view_class is not None:
        return issubclass(view_class, ThesisPermissionRequiredMixin)
    return getattr(cb, "thesis_permission_action", None) is not None


def test_there_are_thesis_routes() -> None:
    assert len(thesis_routes()) >= 3


@pytest.mark.parametrize(("route", "pattern"), thesis_routes(), ids=lambda x: str(x))
def test_every_thesis_route_is_guarded(route: str, pattern: URLPattern) -> None:
    assert _is_guarded(pattern), f"{route} is not guarded by permissions.py"


def test_thesis_id_is_only_accepted_as_uuid() -> None:
    """A route written <thesis_id> or <str:thesis_id> would bypass the uuid converter."""
    loose = [r for r, _ in _walk(get_resolver().url_patterns) if THESIS_URL_KWARG in r]
    assert all(f"<uuid:{THESIS_URL_KWARG}>" in r for r in loose), loose


def _path(route: str, thesis_id: uuid.UUID) -> str:
    """Fill the route: the thesis id, random UUIDs for any other object id, and 1 for
    an integer (a year or a month: the guard answers before the view reads it)."""
    path = route.replace(f"<uuid:{THESIS_URL_KWARG}>", str(thesis_id))
    path = re.sub(r"<uuid:\w+>", lambda _: str(uuid.uuid4()), path)
    path = re.sub(r"<int:\w+>", "1", path)
    assert "<" not in path, f"unsupported converter in {route}"
    return "/" + path.lstrip("^").rstrip("$")


@pytest.mark.django_db
@pytest.mark.parametrize("role", [r for r in Role if r is not Role.PHD_STUDENT])
def test_removed_member_gets_403_everywhere_with_existing_session(role: Role) -> None:
    student = MembershipFactory.create(role=Role.PHD_STUDENT)
    m = MembershipFactory.create(thesis=student.thesis, role=role)
    client = Client()
    client.force_login(m.user)
    session_key = client.session.session_key

    Membership.objects.filter(pk=m.pk).update(active_until=timezone.now())

    assert client.session.session_key == session_key  # same session, not logged out
    for route, _ in thesis_routes():
        path = _path(route, m.thesis.pk)
        for method in (client.get, client.post):
            response = method(path)
            assert response.status_code == 403, (method.__name__, path, response.status_code)
