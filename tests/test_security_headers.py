"""HTTP security headers and CSP-compatible HTML on a page rendered from base.html."""

import json
from html.parser import HTMLParser

import pytest
from django.test import Client
from django.urls import reverse


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.html_lang: str | None = None
        self.scripts: list[dict[str, str | None]] = []
        self.metas: list[dict[str, str | None]] = []
        self.style_tags = 0
        self.style_attrs = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if "style" in attributes:
            self.style_attrs += 1
        if tag == "html":
            self.html_lang = attributes.get("lang")
        elif tag == "script":
            self.scripts.append(attributes)
        elif tag == "meta":
            self.metas.append(attributes)
        elif tag == "style":
            self.style_tags += 1


@pytest.fixture
def login_page(client: Client) -> tuple[object, _Collector]:
    response = client.get(reverse("login"))
    assert response.status_code == 200
    collector = _Collector()
    collector.feed(response.content.decode())
    return response, collector


def test_security_headers(login_page: tuple[object, _Collector]) -> None:
    response, _ = login_page
    assert response["X-Frame-Options"] == "DENY"  # type: ignore[index]
    assert response["X-Content-Type-Options"] == "nosniff"  # type: ignore[index]
    assert response["Referrer-Policy"] == "same-origin"  # type: ignore[index]
    assert response["Cross-Origin-Opener-Policy"] == "same-origin"  # type: ignore[index]
    csp = response["Content-Security-Policy"]  # type: ignore[index]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp


def test_base_template_is_french(login_page: tuple[object, _Collector]) -> None:
    _, page = login_page
    assert page.html_lang == "fr"


def test_htmx_loaded_locally_and_no_inline_script(login_page: tuple[object, _Collector]) -> None:
    _, page = login_page
    sources = [s.get("src") for s in page.scripts]
    assert all(sources), "inline <script> would be blocked by the CSP"
    assert any(src and src.startswith("/static/") and "htmx" in src for src in sources)


def test_no_inline_style(login_page: tuple[object, _Collector]) -> None:
    _, page = login_page
    assert page.style_tags == 0
    assert page.style_attrs == 0


def test_htmx_config_is_csp_compatible(login_page: tuple[object, _Collector]) -> None:
    _, page = login_page
    meta = next(m for m in page.metas if m.get("name") == "htmx-config")
    config = json.loads(meta["content"] or "{}")
    assert config["allowEval"] is False
    assert config["includeIndicatorStyles"] is False
