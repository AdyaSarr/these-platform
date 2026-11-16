"""Every static file a page asks for exists: a missing one is a 404 in the console
(and, for a font or a stylesheet, a page that silently looks wrong).

- each {% static '...' %} of a template is found by the static finders;
- each url() of a stylesheet resolves, relative to that stylesheet, to a file;
- each <link href>, <script src> and <img src> of rendered pages under STATIC_URL
  is found too (catches a path built in a context processor or a tag)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.staticfiles import finders
from django.test import Client
from django.urls import reverse

from apps.accounts.roles import Role
from apps.accounts.tests.factories import UserFactory

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC = BASE_DIR / "static"

STATIC_TAG = re.compile(r"""\{%\s*static\s+(["'])([^"']+)\1""")
ANY_STATIC_TAG = re.compile(r"\{%\s*static\s+(\S+)")
CSS_URL = re.compile(r"""url\(\s*(["']?)([^"')\s]+)\1\s*\)""")
ASSET = re.compile(r"""<(?:link|script|img|source)\b[^>]*?\b(?:href|src)="([^"]+)\"""", re.I)


def _templates() -> list[Path]:
    paths = list((BASE_DIR / "templates").rglob("*.html"))
    paths += list((BASE_DIR / "apps").glob("*/templates/**/*.html"))
    return sorted(paths)


def _references(source: str) -> list[str]:
    return [path for _quote, path in STATIC_TAG.findall(source)]


def test_every_static_tag_names_an_existing_file() -> None:
    missing = [
        (str(path.relative_to(BASE_DIR)), name)
        for path in _templates()
        for name in _references(path.read_text(encoding="utf-8"))
        if finders.find(name) is None
    ]
    assert missing == []
    assert any(_references(p.read_text(encoding="utf-8")) for p in _templates())


def test_every_static_tag_is_a_literal() -> None:
    """A path computed at render time would escape the check above."""
    dynamic = [
        (str(path.relative_to(BASE_DIR)), arg)
        for path in _templates()
        for arg in ANY_STATIC_TAG.findall(path.read_text(encoding="utf-8"))
        if arg[0] not in "'\""
    ]
    assert dynamic == []


def _css_targets(css: str) -> list[str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [target for _quote, target in CSS_URL.findall(css) if not target.startswith("#")]


@pytest.mark.parametrize(
    "path", sorted(STATIC.rglob("*.css")), ids=lambda p: str(p.relative_to(BASE_DIR))
)
def test_every_css_url_points_to_an_existing_file(path: Path) -> None:
    for target in _css_targets(path.read_text(encoding="utf-8")):
        resolved = (path.parent / target.split("?")[0].split("#")[0]).resolve()
        assert resolved.is_file(), (target, resolved)
        assert STATIC.resolve() in resolved.parents, target


def test_the_scans_see_what_they_must() -> None:
    assert _references("{% static 'css/a.css' %} {% static \"js/b.js\" %}") == [
        "css/a.css",
        "js/b.js",
    ]
    assert _css_targets('a{b:url("../x.woff2")} c{d:url(y.svg)} /* url(z.png) */') == [
        "../x.woff2",
        "y.svg",
    ]
    page = '<link rel="preload" href="/static/a" as="font"><script src="/b" defer>'
    assert ASSET.findall(page) == [
        "/static/a",
        "/b",
    ]


def _assets(html: str) -> list[str]:
    return [url for url in ASSET.findall(html) if url.startswith(settings.STATIC_URL)]


def _assert_found(html: str) -> None:
    assets = _assets(html)
    assert assets
    missing = [url for url in assets if finders.find(url.removeprefix(settings.STATIC_URL)) is None]
    assert missing == []


@pytest.mark.django_db
def test_rendered_pages_ask_only_for_existing_files(client: Client) -> None:
    _assert_found(client.get(reverse("account_login")).content.decode())
    client.force_login(UserFactory.create())
    _assert_found(client.get(reverse("core:home")).content.decode())


@pytest.mark.django_db
def test_a_thesis_page_asks_only_for_existing_files() -> None:
    from apps.ui.tests.helpers import signed_in

    client, thesis = signed_in(Role.PHD_STUDENT)
    response = client.get(reverse("accounts:dashboard", kwargs={"thesis_id": thesis.pk}))
    assert response.status_code == 200
    _assert_found(response.content.decode())
