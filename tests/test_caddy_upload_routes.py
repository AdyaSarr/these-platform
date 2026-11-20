"""The @upload expression of the example Caddyfile covers exactly the upload routes.

Caddy gives a 26 MiB body, and at most two connections to Django, to the routes
of ``@upload`` only; every other route gets 1 MiB (docs/DEPLOYMENT.md, SR-14).
An upload route is one whose view reads ``request.FILES`` (in the source of one
of its classes from ``apps``) or whose ``form_class`` has a file field. The test
fails when an upload route is missing from ``@upload``, when ``@upload`` matches
a route that reads no file, and when one of its alternatives matches no upload
route any more (a route renamed or removed).

The expression is RE2 (Go) for Caddy and read here by Python's ``re``: the same
meaning for the syntax it uses (classes, counted repeats, alternatives, anchors).
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from django import forms
from django.urls import URLPattern, URLResolver, get_resolver
from django.urls.resolvers import RoutePattern

BASE_DIR = Path(__file__).resolve().parent.parent
CADDYFILE = BASE_DIR / "deploy" / "examples" / "Caddyfile"
UPLOAD_LINE = re.compile(r"^\s*@upload path_regexp upload (\S+)\s*$", re.MULTILINE)
# A converter in a route, and a value it accepts.
CONVERTER = re.compile(r"<(?:(\w+):)?\w+>")
SAMPLES = {
    "uuid": "00000000-0000-4000-8000-000000000000",
    "int": "1",
    "str": "x",
    "slug": "x",
    "path": "x/y",
    None: "x",
}
# Known upload routes (positive control: the detection is not vacuous).
KNOWN = {
    "/t/<uuid:thesis_id>/documents/new/",
    "/t/<uuid:thesis_id>/documents/<uuid:document_id>/versions/new/",
    "/t/<uuid:thesis_id>/admin-file/<uuid:item_id>/submit/",
    "/t/<uuid:thesis_id>/travel/missions/<uuid:mission_id>/official-order/",
    "/t/<uuid:thesis_id>/travel/expenses/<uuid:expense_id>/receipt/",
}


def _walk(patterns: list[Any], prefix: str = "/") -> Iterator[tuple[str, Any, bool]]:
    """(route, view, is_a_route_pattern) for every URL of the project."""
    for pattern in patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            yield from _walk(pattern.url_patterns, route)
        elif isinstance(pattern, URLPattern):
            yield route, pattern.callback, isinstance(pattern.pattern, RoutePattern)


def _reads_files(view: Any) -> bool:
    view_class = getattr(view, "view_class", None)
    owners = [view_class] if view_class is not None else [view]
    if view_class is not None:
        owners = [k for k in view_class.__mro__ if k.__module__.startswith("apps.")]
    for owner in owners:
        if owner.__module__.startswith("apps.") and ".FILES" in inspect.getsource(owner):
            return True
    form_class = getattr(view_class, "form_class", None)
    if form_class is not None:
        return any(isinstance(f, forms.FileField) for f in form_class.base_fields.values())
    return False


def _sample(route: str) -> str:
    return CONVERTER.sub(lambda m: SAMPLES[m.group(1)], route)


def _routes() -> tuple[set[str], set[str]]:
    """(upload routes, other routes), as Django route strings with a leading /."""
    upload: set[str] = set()
    other: set[str] = set()
    for route, view, is_route in _walk(get_resolver().url_patterns):
        reads = _reads_files(view)
        if not is_route:
            # A regular expression route (re_path) cannot be sampled here.
            assert not reads, f"an upload view behind a regex route: {route}"
            continue
        (upload if reads else other).add(route)
    return upload, other


def _expression() -> str:
    found = UPLOAD_LINE.findall(CADDYFILE.read_text())
    assert len(found) == 1, f"one @upload line expected in {CADDYFILE}, found {found}"
    return str(found[0])


def _alternatives(expression: str) -> list[str]:
    """Each alternative of the one group, as a whole expression of its own."""
    parts = re.fullmatch(r"(?P<head>[^()]*)\((?P<alts>[^()]*)\)(?P<tail>[^()]*)", expression)
    assert parts is not None, f"expected head(a|b|...)tail: {expression}"
    return [f"{parts['head']}(?:{alt}){parts['tail']}" for alt in parts["alts"].split("|")]


def test_the_upload_views_are_found() -> None:
    upload, other = _routes()
    assert upload == KNOWN, sorted(upload ^ KNOWN)
    assert len(other) > 100  # every other route of the project is walked


def test_every_upload_route_is_in_the_caddy_expression() -> None:
    upload, _other = _routes()
    expression = re.compile(_expression())
    missing = sorted(r for r in upload if not expression.fullmatch(_sample(r)))
    assert not missing, f"upload routes outside @upload (413 above 1 MiB): {missing}"


def test_the_caddy_expression_matches_no_route_without_a_file() -> None:
    _upload, other = _routes()
    expression = re.compile(_expression())
    extra = sorted(r for r in other if expression.fullmatch(_sample(r)))
    assert not extra, f"routes in @upload that read no file (26 MiB for nothing): {extra}"


def test_every_alternative_still_names_an_upload_route() -> None:
    upload, _other = _routes()
    samples = [_sample(r) for r in upload]
    dead = [a for a in _alternatives(_expression()) if not any(re.fullmatch(a, s) for s in samples)]
    assert not dead, f"alternatives of @upload that match no upload route: {dead}"
